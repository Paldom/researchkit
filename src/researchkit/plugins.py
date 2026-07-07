"""Plugin discovery, activation, and the runtime registry.

Trust model (see the README plugin guide): installing a package is the
trust decision — this module's job is activation and provenance, not
sandboxing. A plugin is **active** when it is installed, passes the load
gates (valid manifest, exact API version, no name collisions), and every
env var it declares in ``requires_env`` is set. Missing keys leave it
``inactive``; any gate failure leaves it ``quarantined`` with a reason.
One bad plugin never affects core or other plugins.

Environment rails:

- ``RESEARCHKIT_NO_PLUGINS=1`` — kill switch, only built-ins load (plugin
  code is never imported).
- ``RESEARCHKIT_PLUGINS=dist-a,dist-b`` — only these distributions (PEP 503
  normalized) are considered; everything else installed stays dormant.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from importlib.metadata import entry_points

from packaging.utils import canonicalize_name

from researchkit.plugin_api import (
    PLUGIN_API_VERSION,
    ConnectorSpec,
    PluginManifest,
    ProviderSpec,
    valid_extension_name,
)

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "researchkit.plugins"


@dataclass
class LoadedPlugin:
    """Status record for one discovered plugin distribution."""

    dist: str
    version: str = ""
    origin: str = ""  # git url / path / registry, from direct_url.json
    status: str = "active"  # active | inactive | quarantined | excluded
    reason: str = ""
    providers: tuple[str, ...] = ()
    connectors: tuple[str, ...] = ()


@dataclass
class Registry:
    """Providers and connectors available to this process (builtins + plugins)."""

    providers: dict[str, ProviderSpec] = field(default_factory=dict)
    connectors: dict[str, ConnectorSpec] = field(default_factory=dict)
    plugins: list[LoadedPlugin] = field(default_factory=list)

    @property
    def provider_names(self) -> list[str]:
        return sorted(self.providers)

    @property
    def connector_names(self) -> list[str]:
        return sorted(self.connectors)

    def llm_provider_names(self) -> set[str]:
        return {n for n, s in self.providers.items() if s.is_llm}

    def improver_provider_names(self) -> list[str]:
        return sorted(n for n, s in self.providers.items() if s.supports_improver)

    def plugin_versions(self) -> dict[str, str]:
        """Active plugin provenance for result.json (dist -> version)."""
        return {p.dist: p.version for p in self.plugins if p.status == "active"}


def _dist_origin(dist_name: str) -> str:
    """Best-effort install origin from PEP 610 direct_url.json."""
    import json
    from importlib.metadata import distribution

    try:
        text = distribution(dist_name).read_text("direct_url.json")
        if not text:
            return "registry"
        data = json.loads(text)
        url = str(data.get("url", ""))
        if data.get("vcs_info"):
            commit = data["vcs_info"].get("commit_id", "")[:12]
            return f"{url}@{commit}" if commit else url
        if data.get("dir_info", {}).get("editable"):
            return f"editable {url}"
        return url or "registry"
    except Exception:
        return "registry"


def _missing_env(spec_env: tuple[str, ...]) -> list[str]:
    return [key for key in spec_env if not os.getenv(key)]


def discover_plugins() -> list[tuple[LoadedPlugin, PluginManifest | None]]:
    """Load every eligible plugin's manifest, recording per-dist status.

    Never raises: any failure quarantines that one distribution.
    """
    if os.getenv("RESEARCHKIT_NO_PLUGINS"):
        return []
    pinned_raw = os.getenv("RESEARCHKIT_PLUGINS")
    pinned = (
        {canonicalize_name(p.strip()) for p in pinned_raw.split(",") if p.strip()}
        if pinned_raw is not None
        else None
    )

    results: list[tuple[LoadedPlugin, PluginManifest | None]] = []
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        dist_name = ep.dist.name if ep.dist else ep.name
        record = LoadedPlugin(dist=dist_name)
        try:
            record.version = ep.dist.version if ep.dist else ""
            record.origin = _dist_origin(dist_name)
        except Exception:  # provenance is best-effort
            logger.debug("No provenance for %s", dist_name, exc_info=True)

        if pinned is not None and canonicalize_name(dist_name) not in pinned:
            record.status = "excluded"
            record.reason = "not in RESEARCHKIT_PLUGINS"
            results.append((record, None))
            continue

        try:
            manifest = ep.load()
        except Exception as e:
            record.status = "quarantined"
            record.reason = f"import failed: {e!r}"[:200]
            logger.warning("Plugin %s quarantined: %s", dist_name, record.reason)
            results.append((record, None))
            continue

        if not isinstance(manifest, PluginManifest):
            record.status = "quarantined"
            record.reason = "entry point did not resolve to a PluginManifest"
            results.append((record, None))
            continue
        if manifest.api_version != PLUGIN_API_VERSION:
            record.status = "quarantined"
            record.reason = (
                f"plugin API {manifest.api_version} != core {PLUGIN_API_VERSION}"
            )
            results.append((record, None))
            continue

        results.append((record, manifest))
    return results


def _register_specs(
    registry: Registry,
    record: LoadedPlugin,
    manifest: PluginManifest,
    builtin_names: set[str],
    claimed: dict[str, str],
) -> None:
    """Apply one active plugin's specs, enforcing collision rules."""
    provider_names: list[str] = []
    connector_names: list[str] = []
    specs: list[tuple[str, ProviderSpec | ConnectorSpec]] = [
        *(("provider", s) for s in manifest.providers),
        *(("connector", s) for s in manifest.connectors),
    ]
    for kind, spec in specs:
        if not valid_extension_name(spec.name):
            record.status = "quarantined"
            record.reason = f"invalid {kind} name {spec.name!r}"
            break
        if spec.name in builtin_names:
            record.status = "quarantined"
            record.reason = f"{kind} name {spec.name!r} shadows a built-in"
            break
        other = claimed.get(spec.name)
        if other and other != record.dist:
            record.status = "quarantined"
            record.reason = (
                f"{kind} name {spec.name!r} also claimed by {other} — "
                "both plugins disabled"
            )
            # retroactively quarantine the earlier claimant
            for p in registry.plugins:
                if p.dist == other:
                    p.status = "quarantined"
                    p.reason = record.reason
            for name in list(registry.providers):
                if claimed.get(name) == other:
                    del registry.providers[name]
            for name in list(registry.connectors):
                if claimed.get(name) == other:
                    del registry.connectors[name]
            break
    if record.status == "quarantined":
        logger.warning("Plugin %s quarantined: %s", record.dist, record.reason)
        return

    for spec in manifest.providers:
        registry.providers[spec.name] = spec
        claimed[spec.name] = record.dist
        provider_names.append(spec.name)
    for spec in manifest.connectors:
        registry.connectors[spec.name] = spec
        claimed[spec.name] = record.dist
        connector_names.append(spec.name)
    record.providers = tuple(provider_names)
    record.connectors = tuple(connector_names)


def build_registry() -> Registry:
    """Builtins plus every active plugin, with per-plugin isolation."""
    from researchkit.plugins_builtin import builtin_connectors, builtin_providers

    registry = Registry()
    for provider_spec in builtin_providers():
        registry.providers[provider_spec.name] = provider_spec
    for connector_spec in builtin_connectors():
        registry.connectors[connector_spec.name] = connector_spec
    builtin_names = set(registry.providers) | set(registry.connectors)

    claimed: dict[str, str] = {}
    for record, manifest in discover_plugins():
        registry.plugins.append(record)
        if manifest is None:
            continue
        declared = [
            *(s.requires_env for s in manifest.providers),
            *(s.requires_env for s in manifest.connectors),
        ]
        missing = sorted({key for env in declared for key in _missing_env(env)})
        if missing:
            record.status = "inactive"
            record.reason = f"set {', '.join(missing)} to activate"
            continue
        _register_specs(registry, record, manifest, builtin_names, claimed)

    return registry


def default_site_research_sites() -> list[str]:
    """Every active connector — installed plugins enrich runs by default."""
    return list(get_registry().connector_names)


_registry: Registry | None = None


def get_registry(*, refresh: bool = False) -> Registry:
    """Process-wide registry, built lazily on first use."""
    global _registry
    if _registry is None or refresh:
        _registry = build_registry()
    return _registry
