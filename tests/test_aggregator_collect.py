"""Tests for the concurrent provider fan-out (review M14, L1).

Previously the heart of the product — collect_async + _create_provider routing —
had zero coverage; the "integration" tests patched collect_async out entirely.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from researchkit.aggregator import InsightAggregator
from researchkit.providers import (
    AntigravityProvider,
    ClaudeProvider,
    CodexProvider,
    GrokCliProvider,
    OpenAIProvider,
)
from researchkit.providers.base import ProviderResult, Source, SourceType
from researchkit.system_config import EffectiveModels


class _StubProvider:
    def __init__(self, name: str, mode: str) -> None:
        self.provider_name = name
        self._name = name
        self._mode = mode

    def fetch_insights(self, topic: str, days: int) -> ProviderResult:
        if self._mode == "ok":
            return ProviderResult(
                provider=self._name,
                model="stub",
                raw_text="some findings",
                sources=[Source(url="https://x/1", source_type=SourceType.WEB)],
            )
        if self._mode == "error":
            return ProviderResult(
                provider=self._name, model="stub", raw_text="", error="soft failure"
            )
        raise RuntimeError("provider constructor/impl blew up")


def test_collect_async_keeps_all_providers_in_order() -> None:
    """A succeeding, a soft-failing, and a *raising* provider all appear in the
    result, in the requested order — the raising one is no longer dropped under
    an 'unknown' key. (Review M14, L1.)"""
    agg = InsightAggregator()
    stubs = {
        "openai": _StubProvider("openai", "ok"),
        "gemini": _StubProvider("gemini", "error"),
        "grok": _StubProvider("grok", "raise"),
    }
    agg._create_provider = lambda name, keywords=None: stubs[name]  # type: ignore[assignment]

    results = agg.collect_sync("some topic", 7, ["openai", "gemini", "grok"])

    assert [r.provider for r in results] == ["openai", "gemini", "grok"]
    assert results[0].is_success and results[0].raw_text == "some findings"
    assert not results[1].is_success and results[1].error == "soft failure"
    # The raising provider is captured, not silently dropped.
    assert not results[2].is_success
    assert "blew up" in (results[2].error or "")


def test_collect_async_empty_provider_list() -> None:
    agg = InsightAggregator()
    assert agg.collect_sync("t", 7, []) == []


def _effective(**overrides: object) -> EffectiveModels:
    base = EffectiveModels(
        openai="gpt-5.4-mini",
        gemini="gemini-3.5-flash",
        grok="grok-4.3",
        perplexity="sonar",
        tavily="tavily-search",
        claude="claude-sonnet-4-6",
        github="gpt-5.4-mini",
        glm="glm-5.2",
        kimi="kimi-k2.6",
        summarizer="gemini-3.5-flash",
        site_summarizer="gemini-3-flash-preview",
        improver="gpt-5.4-mini",
        reasoning_effort="low",
        perplexity_search_type="fast",
        tavily_search_depth="fast",
        claude_max_budget=5.0,
        preset_name="test",
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_create_provider_routes_codex_and_agy_and_deep() -> None:
    """codex:/agy:/deep: specs route to the CLI-backed provider classes; plain
    ids route to the API provider. (Review M14.)"""
    agg = InsightAggregator(
        effective_models=_effective(
            openai="codex:gpt-5.5",
            gemini="agy:gemini-3.5-flash",
            grok="grokcli:grok-build",
            claude="deep:claude-sonnet-5",
        )
    )
    assert isinstance(agg._create_provider("openai"), CodexProvider)
    assert isinstance(agg._create_provider("gemini"), AntigravityProvider)
    grok = agg._create_provider("grok")
    assert isinstance(grok, GrokCliProvider)
    assert grok.reasoning_effort == "low"  # preset knob flows through options
    claude = agg._create_provider("claude")
    assert isinstance(claude, ClaudeProvider) and claude.deep_research is True


def test_create_provider_routes_plain_ids() -> None:
    agg = InsightAggregator(effective_models=_effective())
    assert isinstance(agg._create_provider("openai"), OpenAIProvider)
    claude = agg._create_provider("claude")
    assert isinstance(claude, ClaudeProvider) and claude.deep_research is False


def test_create_provider_unknown_raises() -> None:
    agg = InsightAggregator()
    with pytest.raises(ValueError):
        agg._create_provider("nope")
