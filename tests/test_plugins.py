"""Tests for plugin discovery, activation, and the registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

import researchkit.plugins as plugins_mod
from researchkit.plugin_api import (
    PLUGIN_API_VERSION,
    BaseProvider,
    ConnectorSpec,
    PluginManifest,
    ProviderContext,
    ProviderResult,
    ProviderSpec,
)
from researchkit.plugins import build_registry
from researchkit.site_research.connectors.base import BaseSiteConnector


class DummyProvider(BaseProvider):
    provider_name = "dummy"
    model_name = "dummy-model"

    def __init__(self, ctx: ProviderContext) -> None:
        self.ctx = ctx

    def fetch_insights(self, topic: str, days: int) -> ProviderResult:
        return ProviderResult(
            provider="dummy", model=self.ctx.model, raw_text="ok", sources=[]
        )


class DummyConnector(BaseSiteConnector):
    site_name = "dummysite"

    def search(self, query: str, published_after: Any, limit: int) -> list[Any]:
        return []

    def summarize(self, topic: str, item: Any) -> Any:
        raise NotImplementedError

    def popularity_score(self, item: Any) -> float:
        return 0.0


def make_manifest(
    api_version: int = PLUGIN_API_VERSION,
    provider_name: str = "dummy",
    connector_name: str = "dummysite",
    requires_env: tuple[str, ...] = (),
) -> PluginManifest:
    return PluginManifest(
        api_version=api_version,
        providers=(
            ProviderSpec(
                provider_name,
                lambda ctx: DummyProvider(ctx),
                is_llm=True,
                requires_env=requires_env,
            ),
        ),
        connectors=(ConnectorSpec(connector_name, lambda ctx: DummyConnector()),),
    )


@dataclass
class FakeDist:
    name: str
    version: str = "1.2.3"


class FakeEntryPoint:
    def __init__(self, dist_name: str, payload: Any, name: str = "plugin") -> None:
        self.name = name
        self.dist = FakeDist(dist_name)
        self._payload = payload

    def load(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


@pytest.fixture
def fake_eps(monkeypatch: pytest.MonkeyPatch):
    """Install a controllable entry-point list."""
    eps: list[FakeEntryPoint] = []
    monkeypatch.setattr(plugins_mod, "entry_points", lambda group: list(eps))
    monkeypatch.setattr(plugins_mod, "_dist_origin", lambda dist: "test-origin")
    return eps


def test_active_plugin_registers_extensions(fake_eps: list) -> None:
    fake_eps.append(FakeEntryPoint("my-plugin", make_manifest()))
    registry = build_registry()
    assert "dummy" in registry.providers
    assert "dummysite" in registry.connectors
    assert registry.plugins[0].status == "active"
    assert registry.plugins[0].providers == ("dummy",)
    assert registry.plugin_versions() == {"my-plugin": "1.2.3"}
    assert "dummy" in registry.llm_provider_names()


def test_missing_env_key_leaves_plugin_inactive(
    fake_eps: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SOME_PLUGIN_KEY", raising=False)
    fake_eps.append(
        FakeEntryPoint("my-plugin", make_manifest(requires_env=("SOME_PLUGIN_KEY",)))
    )
    registry = build_registry()
    assert "dummy" not in registry.providers
    record = registry.plugins[0]
    assert record.status == "inactive"
    assert "SOME_PLUGIN_KEY" in record.reason

    monkeypatch.setenv("SOME_PLUGIN_KEY", "value")
    registry = build_registry()
    assert registry.plugins[0].status == "active"
    assert "dummy" in registry.providers


def test_kill_switch_and_pinning(
    fake_eps: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_eps.append(FakeEntryPoint("my-plugin", make_manifest()))

    monkeypatch.setenv("RESEARCHKIT_NO_PLUGINS", "1")
    registry = build_registry()
    assert registry.plugins == [] and "dummy" not in registry.providers
    monkeypatch.delenv("RESEARCHKIT_NO_PLUGINS")

    monkeypatch.setenv("RESEARCHKIT_PLUGINS", "Other_Dist")
    registry = build_registry()
    assert registry.plugins[0].status == "excluded"

    # PEP 503 normalization: My-Plugin matches my-plugin
    monkeypatch.setenv("RESEARCHKIT_PLUGINS", "My_Plugin")
    registry = build_registry()
    assert registry.plugins[0].status == "active"


def test_broken_plugins_are_quarantined_individually(fake_eps: list) -> None:
    fake_eps.append(FakeEntryPoint("import-boom", ImportError("boom")))
    fake_eps.append(FakeEntryPoint("not-manifest", object()))
    fake_eps.append(FakeEntryPoint("wrong-api", make_manifest(api_version=99)))
    fake_eps.append(
        FakeEntryPoint(
            "good-plugin",
            make_manifest(provider_name="dummy2", connector_name="dummysite2"),
        )
    )
    registry = build_registry()
    by_dist = {p.dist: p for p in registry.plugins}
    assert by_dist["import-boom"].status == "quarantined"
    assert by_dist["not-manifest"].status == "quarantined"
    assert "99" in by_dist["wrong-api"].reason
    assert by_dist["good-plugin"].status == "active"


def test_builtin_shadowing_is_rejected(fake_eps: list) -> None:
    fake_eps.append(FakeEntryPoint("shady", make_manifest(provider_name="openai")))
    registry = build_registry()
    assert registry.plugins[0].status == "quarantined"
    assert "shadows a built-in" in registry.plugins[0].reason
    # the built-in stays intact
    assert registry.providers["openai"].factory is not None


def test_name_collision_quarantines_both(fake_eps: list) -> None:
    fake_eps.append(FakeEntryPoint("plugin-a", make_manifest()))
    fake_eps.append(FakeEntryPoint("plugin-b", make_manifest()))
    registry = build_registry()
    statuses = {p.dist: p.status for p in registry.plugins}
    assert statuses == {"plugin-a": "quarantined", "plugin-b": "quarantined"}
    assert "dummy" not in registry.providers
    assert "dummysite" not in registry.connectors


def test_invalid_extension_name_quarantined(fake_eps: list) -> None:
    fake_eps.append(
        FakeEntryPoint("bad-name", make_manifest(provider_name="Bad Name!"))
    )
    registry = build_registry()
    assert registry.plugins[0].status == "quarantined"
    assert "invalid provider name" in registry.plugins[0].reason


def test_plugin_provider_constructs_through_aggregator(fake_eps: list) -> None:
    from researchkit.aggregator import InsightAggregator
    from researchkit.system_config import EffectiveModels

    fake_eps.append(FakeEntryPoint("my-plugin", make_manifest()))
    registry = build_registry()

    em = EffectiveModels(
        openai="o",
        gemini="g",
        grok="k",
        perplexity="p",
        tavily="t",
        claude="c",
        github="h",
        glm="l",
        summarizer="s",
        site_summarizer="ss",
        improver="i",
        reasoning_effort="low",
        perplexity_search_type="fast",
        tavily_search_depth="fast",
        claude_max_budget=1.0,
        preset_name="test",
        plugin_models={"dummy": "dummy-model-v2"},
        plugin_options={"dummy": {"knob": 42}},
    )
    agg = InsightAggregator(effective_models=em, registry=registry)
    provider = agg._create_provider("dummy")
    assert isinstance(provider, DummyProvider)
    assert provider.ctx.model == "dummy-model-v2"
    assert provider.ctx.options["knob"] == 42
