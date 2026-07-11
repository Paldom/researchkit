"""The blessed plugin API surface.

Plugins import **only** from this module (and ``researchkit.testing`` in
their tests). Everything else in researchkit is private and may change
without notice; ``researchkit.testing.check_blessed_imports`` enforces this
for plugin packages.

A plugin is a normal Python package exposing one entry point in the group
``researchkit.plugins`` that resolves to a module-level
:class:`PluginManifest`:

    [project.entry-points."researchkit.plugins"]
    myplugin = "my_pkg.plugin:MANIFEST"

Activation is key-based: an installed plugin becomes active when every env
var it declares in ``requires_env`` is set — the same graceful pattern
built-in providers follow. See the README's plugin guide.

Compatibility: ``api_version`` must equal :data:`PLUGIN_API_VERSION`
exactly. Additive changes (new optional fields with defaults) never bump
the version.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from researchkit.council import complete_via_spec, is_cli_backed_spec
from researchkit.network_retry import with_network_retry
from researchkit.providers.base import (
    BaseProvider,
    ProviderResult,
    Source,
    SourceType,
    provider_http_timeout,
)
from researchkit.site_research.connectors.base import BaseSiteConnector
from researchkit.site_research.types import (
    ExtractedFact,
    SiteItem,
    SiteItemSummary,
    TopicRelevance,
)

__all__ = [
    "PLUGIN_API_VERSION",
    "BaseProvider",
    "BaseSiteConnector",
    "ConnectorContext",
    "ConnectorSpec",
    "ExtractedFact",
    "PluginManifest",
    "ProviderContext",
    "ProviderResult",
    "ProviderSpec",
    "SiteItem",
    "SiteItemSummary",
    "Source",
    "SourceType",
    "TopicRelevance",
    "complete_via_spec",
    "is_cli_backed_spec",
    "provider_http_timeout",
    "with_network_retry",
]

PLUGIN_API_VERSION = 1

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")


@dataclass(frozen=True)
class ProviderContext:
    """Everything a provider factory may see.

    Frozen and JSON-serializable by design (a future out-of-process host
    must be able to send it across a boundary); additive-only within an
    API version.
    """

    model: str
    sources: frozenset[str]
    keywords: tuple[str, ...] = ()
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConnectorContext:
    """Everything a connector factory may see."""

    summarizer_model: str
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderSpec:
    """Registers one research provider under a short name."""

    name: str
    factory: Callable[[ProviderContext], BaseProvider]
    is_llm: bool = False  # participates in keyword generation / self-summary
    supports_improver: bool = False  # usable by improve-topic / keyword CLI
    default_model: str = ""  # used when no models.<name> preset entry exists
    requires_env: tuple[str, ...] = ()  # diagnostics + activation gate


@dataclass(frozen=True)
class ConnectorSpec:
    """Registers one site-research connector under a short name."""

    name: str
    factory: Callable[[ConnectorContext], BaseSiteConnector]
    default_max_items: int = 5
    requires_env: tuple[str, ...] = ()


@dataclass(frozen=True)
class PluginManifest:
    """What a plugin's entry point resolves to.

    Deliberately carries no name/version: ``pyproject.toml`` is the
    manifest of record, and provenance comes from installed distribution
    metadata at load time, so it can never disagree with what is installed.
    """

    api_version: int
    providers: tuple[ProviderSpec, ...] = ()
    connectors: tuple[ConnectorSpec, ...] = ()


def valid_extension_name(name: str) -> bool:
    """True when ``name`` may be used as a provider/connector registry key."""
    return bool(_NAME_RE.fullmatch(name))
