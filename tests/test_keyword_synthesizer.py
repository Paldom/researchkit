"""Tests for KeywordSynthesizer and parse_keyword_json."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from researchkit.keyword_synthesizer import KeywordSynthesizer, parse_keyword_json
from researchkit.providers.base import ProviderResult, Source, SourceType


def _make_result(
    provider: str = "openai",
    raw_text: str = "Some research output.",
    titles: list[str] | None = None,
    error: str | None = None,
) -> ProviderResult:
    sources = [
        Source(url=f"https://example.com/{i}", title=t, source_type=SourceType.WEB)
        for i, t in enumerate(titles or [])
    ]
    return ProviderResult(
        provider=provider,
        model="gpt-test",
        raw_text=raw_text if not error else "",
        sources=sources,
        error=error,
    )


# ------------------------------------------------------------------
# parse_keyword_json (module-level utility)
# ------------------------------------------------------------------


class TestParseKeywordJson:
    def test_parses_clean_json(self) -> None:
        response = json.dumps(
            {
                "keywords": [
                    "claude opus 4 review",
                    "claude vs gpt-5",
                    "claude code agents",
                ]
            }
        )
        result = parse_keyword_json(response, count=10)
        assert result == [
            "claude opus 4 review",
            "claude vs gpt-5",
            "claude code agents",
        ]

    def test_extracts_json_from_noisy_text(self) -> None:
        response = (
            "Sure, here you go:\n```json\n"
            '{"keywords": ["langchain rag tutorial", "llm eval frameworks"]}\n```'
        )
        result = parse_keyword_json(response, count=10)
        assert result == ["langchain rag tutorial", "llm eval frameworks"]

    def test_filters_short_and_dedupes(self) -> None:
        response = json.dumps(
            {
                "keywords": [
                    "ai",  # too short (1 word)
                    "claude opus review",
                    "Claude Opus Review",  # dedup case-insensitive
                    "",  # empty
                    "  langchain rag tutorial  ",  # trimmed
                ]
            }
        )
        result = parse_keyword_json(response, count=10)
        assert result == ["claude opus review", "langchain rag tutorial"]

    def test_caps_at_count(self) -> None:
        response = json.dumps({"keywords": [f"keyword phrase {i}" for i in range(20)]})
        result = parse_keyword_json(response, count=5)
        assert len(result) == 5

    def test_returns_empty_on_invalid_json(self) -> None:
        assert parse_keyword_json("not json at all", count=10) == []

    def test_returns_empty_on_missing_keywords_field(self) -> None:
        response = json.dumps({"other_field": ["a", "b"]})
        assert parse_keyword_json(response, count=10) == []


# ------------------------------------------------------------------
# KeywordSynthesizer._parse_keywords (delegates to parse_keyword_json)
# ------------------------------------------------------------------


class TestParseKeywords:
    def test_parses_clean_json(self) -> None:
        synth = KeywordSynthesizer(model="gpt-test")
        response = json.dumps(
            {
                "keywords": [
                    "claude opus 4 review",
                    "claude vs gpt-5",
                    "claude code agents",
                ]
            }
        )
        result = synth._parse_keywords(response, count=10)
        assert result == [
            "claude opus 4 review",
            "claude vs gpt-5",
            "claude code agents",
        ]

    def test_filters_short_and_dedupes(self) -> None:
        synth = KeywordSynthesizer(model="gpt-test")
        response = json.dumps(
            {
                "keywords": [
                    "ai",
                    "claude opus review",
                    "Claude Opus Review",
                    "",
                    "  langchain rag tutorial  ",
                ]
            }
        )
        result = synth._parse_keywords(response, count=10)
        assert result == ["claude opus review", "langchain rag tutorial"]

    def test_caps_at_count(self) -> None:
        synth = KeywordSynthesizer(model="gpt-test")
        response = json.dumps({"keywords": [f"keyword phrase {i}" for i in range(20)]})
        result = synth._parse_keywords(response, count=5)
        assert len(result) == 5

    def test_returns_empty_on_invalid_json(self) -> None:
        synth = KeywordSynthesizer(model="gpt-test")
        result = synth._parse_keywords("not json at all", count=10)
        assert result == []

    def test_returns_empty_on_missing_keywords_field(self) -> None:
        synth = KeywordSynthesizer(model="gpt-test")
        response = json.dumps({"other_field": ["a", "b"]})
        result = synth._parse_keywords(response, count=10)
        assert result == []


# ------------------------------------------------------------------
# _build_context
# ------------------------------------------------------------------


class TestBuildContext:
    def test_includes_meta_summary(self) -> None:
        synth = KeywordSynthesizer(model="gpt-test")
        ctx = synth._build_context(
            provider_results=[],
            individual_summaries={},
            meta_summary="The community is excited about Claude Opus 4.6.",
        )
        assert "Meta-summary" in ctx
        assert "Claude Opus 4.6" in ctx

    def test_includes_individual_summaries(self) -> None:
        synth = KeywordSynthesizer(model="gpt-test")
        ctx = synth._build_context(
            provider_results=[],
            individual_summaries={
                "openai": "OpenAI says Anthropic shipped a new SDK.",
                "gemini": "Gemini notes the agent harness changes.",
            },
            meta_summary="",
        )
        assert "openai" in ctx
        assert "gemini" in ctx
        assert "Anthropic shipped" in ctx
        assert "agent harness" in ctx

    def test_includes_dedup_source_titles(self) -> None:
        synth = KeywordSynthesizer(model="gpt-test")
        results = [
            _make_result(
                provider="openai",
                titles=["Claude Opus 4.6 Released", "Anthropic Claude Code Update"],
            ),
            _make_result(
                provider="gemini",
                titles=["claude opus 4.6 released", "Gemini 3 vs Claude"],
            ),
        ]
        ctx = synth._build_context(
            provider_results=results,
            individual_summaries={},
            meta_summary="",
        )
        assert ctx.count("Claude Opus 4.6 Released") == 1
        assert "Anthropic Claude Code Update" in ctx
        assert "Gemini 3 vs Claude" in ctx

    def test_skips_failed_results(self) -> None:
        synth = KeywordSynthesizer(model="gpt-test")
        results = [
            _make_result(provider="openai", titles=["Good Title"]),
            _make_result(provider="grok", error="API error"),
        ]
        ctx = synth._build_context(
            provider_results=results,
            individual_summaries={},
            meta_summary="",
        )
        assert "Good Title" in ctx

    def test_empty_context_when_nothing_to_use(self) -> None:
        synth = KeywordSynthesizer(model="gpt-test")
        ctx = synth._build_context(
            provider_results=[],
            individual_summaries={},
            meta_summary="",
        )
        assert ctx == ""


# ------------------------------------------------------------------
# synthesize (single-LLM, legacy path)
# ------------------------------------------------------------------


class TestSynthesize:
    def test_returns_empty_on_empty_context(self) -> None:
        synth = KeywordSynthesizer(model="gpt-test")
        result = synth.synthesize(
            topic="anything",
            days=7,
            provider_results=[],
            individual_summaries={},
            meta_summary="",
        )
        assert result == []

    def test_returns_empty_on_llm_failure(self) -> None:
        synth = KeywordSynthesizer(model="gpt-test")
        with patch.object(synth, "_call_openai", side_effect=RuntimeError("api down")):
            result = synth.synthesize(
                topic="claude",
                days=7,
                provider_results=[_make_result(titles=["Claude Released"])],
                individual_summaries={},
                meta_summary="Claude is new.",
            )
        assert result == []

    def test_happy_path(self) -> None:
        synth = KeywordSynthesizer(model="gpt-test")
        canned = json.dumps(
            {
                "keywords": [
                    "claude opus 4.6 review",
                    "anthropic agent harness",
                    "claude code skills",
                ]
            }
        )
        with patch.object(synth, "_call_openai", return_value=canned):
            result = synth.synthesize(
                topic="claude",
                days=7,
                provider_results=[_make_result(titles=["Claude Opus 4.6 Released"])],
                individual_summaries={"openai": "Claude got a new release."},
                meta_summary="Lots of buzz around Claude Opus 4.6.",
            )
        assert result == [
            "claude opus 4.6 review",
            "anthropic agent harness",
            "claude code skills",
        ]


# ------------------------------------------------------------------
# synthesize_from_provider_keywords (multi-provider path)
# ------------------------------------------------------------------


class TestSynthesizeFromProviderKeywords:
    def test_returns_empty_when_no_provider_keywords(self) -> None:
        synth = KeywordSynthesizer(model="gpt-test")
        assert synth.synthesize_from_provider_keywords("t", 7, {}) == []

    def test_returns_empty_when_all_lists_empty(self) -> None:
        synth = KeywordSynthesizer(model="gpt-test")
        result = synth.synthesize_from_provider_keywords(
            "t", 7, {"openai": [], "gemini": []}
        )
        assert result == []

    def test_happy_path(self) -> None:
        synth = KeywordSynthesizer(model="gpt-test")
        provider_keywords = {
            "openai": ["claude opus review", "anthropic agent sdk"],
            "gemini": ["claude code tutorial", "anthropic agent sdk"],
        }
        canned = json.dumps(
            {
                "keywords": [
                    "anthropic agent sdk overview",
                    "claude opus review comparison",
                    "claude code tutorial guide",
                ]
            }
        )
        with patch.object(synth, "_call_openai", return_value=canned) as mock:
            result = synth.synthesize_from_provider_keywords(
                "claude", 7, provider_keywords, count=10
            )

        assert len(result) == 3
        assert "anthropic agent sdk overview" in result
        # Verify the synthesis prompt includes provider names
        call_args = mock.call_args
        user_prompt = (
            call_args[1]["user_prompt"]
            if "user_prompt" in call_args[1]
            else call_args[0][1]
        )
        assert "openai" in user_prompt
        assert "gemini" in user_prompt

    def test_fallback_on_llm_failure(self) -> None:
        synth = KeywordSynthesizer(model="gpt-test")
        provider_keywords = {
            "openai": ["claude opus review", "anthropic agent sdk"],
            "gemini": ["claude code tutorial", "gemini flash update"],
        }
        with patch.object(synth, "_call_openai", side_effect=RuntimeError("api down")):
            result = synth.synthesize_from_provider_keywords(
                "claude", 7, provider_keywords
            )
        # Should fall back to _fallback_merge
        assert len(result) == 4
        assert "claude opus review" in result
        assert "gemini flash update" in result

    def test_fallback_when_parse_returns_empty(self) -> None:
        synth = KeywordSynthesizer(model="gpt-test")
        provider_keywords = {
            "openai": ["claude opus review", "anthropic agent sdk"],
        }
        # Return invalid JSON so parse_keyword_json returns []
        with patch.object(synth, "_call_openai", return_value="garbage"):
            result = synth.synthesize_from_provider_keywords(
                "claude", 7, provider_keywords
            )
        # Should fall back to _fallback_merge
        assert result == ["claude opus review", "anthropic agent sdk"]


# ------------------------------------------------------------------
# _fallback_merge
# ------------------------------------------------------------------


class TestFallbackMerge:
    def test_deduplicates_across_providers(self) -> None:
        provider_keywords = {
            "openai": ["claude opus review", "anthropic sdk"],
            "gemini": ["Claude Opus Review", "gemini flash update"],
        }
        result = KeywordSynthesizer._fallback_merge(provider_keywords, count=10)
        # "Claude Opus Review" is deduped (case-insensitive) with "claude opus review"
        assert len(result) == 3
        assert result[0] == "claude opus review"

    def test_caps_at_count(self) -> None:
        provider_keywords = {
            "openai": [f"keyword phrase {i}" for i in range(10)],
            "gemini": [f"other keyword {i}" for i in range(10)],
        }
        result = KeywordSynthesizer._fallback_merge(provider_keywords, count=5)
        assert len(result) == 5

    def test_filters_single_word(self) -> None:
        provider_keywords = {
            "openai": ["ai", "claude opus review"],
        }
        result = KeywordSynthesizer._fallback_merge(provider_keywords, count=10)
        assert result == ["claude opus review"]

    def test_handles_empty_input(self) -> None:
        assert KeywordSynthesizer._fallback_merge({}, count=10) == []


# ------------------------------------------------------------------
# from_effective_models
# ------------------------------------------------------------------


class TestFromEffectiveModels:
    def test_uses_improver_from_effective_models(self) -> None:
        class _FakeEffective:
            improver = "gpt-improver-test"

        synth = KeywordSynthesizer.from_effective_models(_FakeEffective())  # type: ignore[arg-type]
        assert synth.model == "gpt-improver-test"

    def test_uses_default_when_none(self) -> None:
        synth = KeywordSynthesizer.from_effective_models(None)
        assert synth.model == KeywordSynthesizer.DEFAULT_MODEL


# ------------------------------------------------------------------
# Aggregator integration tests
# ------------------------------------------------------------------


class TestAggregatorIntegration:
    """Integration: aggregator multi-provider keyword generation flow."""

    @pytest.mark.asyncio
    async def test_user_keywords_skip_synthesis(self) -> None:
        from researchkit.aggregator import InsightAggregator

        aggregator = InsightAggregator(site_research_enabled=True)

        async def fake_collect_async(topic, days, providers, progress=None, **kwargs):
            return [_make_result(provider="openai", titles=["A Title"])]

        async def fake_run_site_research(topic, keywords, days, progress=None):
            fake_run_site_research.called_with = list(keywords)  # type: ignore[attr-defined]
            return None

        with (
            patch.object(aggregator, "collect_async", side_effect=fake_collect_async),
            patch.object(
                aggregator, "_run_site_research", side_effect=fake_run_site_research
            ),
            patch.object(
                aggregator.summarizer,
                "create_meta_summary",
                return_value="meta",
            ),
            patch.object(
                aggregator,
                "_generate_keywords_from_providers",
                return_value={},
            ) as gen_mock,
        ):
            bundle = await aggregator.collect_and_summarize_async(
                topic="t",
                days=7,
                providers=["openai"],
                keywords=["my seed one", "my seed two"],
            )

        gen_mock.assert_not_called()
        assert bundle.synthesized_keywords is None
        assert bundle.provider_keywords is None
        assert bundle.keywords == ["my seed one", "my seed two"]
        assert fake_run_site_research.called_with == ["my seed one", "my seed two"]  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_no_user_keywords_triggers_multi_provider_synthesis(self) -> None:
        from researchkit.aggregator import InsightAggregator

        aggregator = InsightAggregator(site_research_enabled=True)

        async def fake_collect_async(topic, days, providers, progress=None, **kwargs):
            return [_make_result(provider="openai", titles=["A Title"])]

        async def fake_run_site_research(topic, keywords, days, progress=None):
            fake_run_site_research.called_with = list(keywords)  # type: ignore[attr-defined]
            return None

        fake_provider_keywords = {
            "openai": ["claude opus review", "anthropic sdk tutorial"],
        }

        with (
            patch.object(aggregator, "collect_async", side_effect=fake_collect_async),
            patch.object(
                aggregator, "_run_site_research", side_effect=fake_run_site_research
            ),
            patch.object(
                aggregator.summarizer,
                "create_meta_summary",
                return_value="meta",
            ),
            patch.object(
                aggregator,
                "_generate_keywords_from_providers",
                return_value=fake_provider_keywords,
            ) as gen_mock,
            patch(
                "researchkit.keyword_synthesizer.KeywordSynthesizer"
                ".synthesize_from_provider_keywords",
                return_value=["synth one", "synth two"],
            ) as synth_mock,
        ):
            bundle = await aggregator.collect_and_summarize_async(
                topic="t",
                days=7,
                providers=["openai"],
                keywords=[],
            )

        gen_mock.assert_called_once()
        synth_mock.assert_called_once()
        assert bundle.synthesized_keywords == ["synth one", "synth two"]
        assert bundle.provider_keywords == fake_provider_keywords
        assert bundle.keywords == []
        assert fake_run_site_research.called_with == ["synth one", "synth two"]  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_fallback_to_single_llm_when_no_provider_keywords(self) -> None:
        """When all providers fail keyword gen, fall back to single-LLM."""
        from researchkit.aggregator import InsightAggregator

        aggregator = InsightAggregator(site_research_enabled=True)

        async def fake_collect_async(topic, days, providers, progress=None, **kwargs):
            return [_make_result(provider="openai", titles=["A Title"])]

        async def fake_run_site_research(topic, keywords, days, progress=None):
            return None

        with (
            patch.object(aggregator, "collect_async", side_effect=fake_collect_async),
            patch.object(
                aggregator, "_run_site_research", side_effect=fake_run_site_research
            ),
            patch.object(
                aggregator.summarizer,
                "create_meta_summary",
                return_value="meta",
            ),
            patch.object(
                aggregator,
                "_generate_keywords_from_providers",
                return_value={},  # All providers failed
            ),
            patch(
                "researchkit.keyword_synthesizer.KeywordSynthesizer.synthesize",
                return_value=["fallback kw one", "fallback kw two"],
            ) as fallback_mock,
        ):
            bundle = await aggregator.collect_and_summarize_async(
                topic="t",
                days=7,
                providers=["openai"],
                keywords=[],
            )

        fallback_mock.assert_called_once()
        assert bundle.synthesized_keywords == ["fallback kw one", "fallback kw two"]

    @pytest.mark.asyncio
    async def test_site_research_disabled_skips_synthesis(self) -> None:
        from researchkit.aggregator import InsightAggregator

        aggregator = InsightAggregator(site_research_enabled=False)

        async def fake_collect_async(topic, days, providers, progress=None, **kwargs):
            return [_make_result(provider="openai", titles=["A Title"])]

        async def fake_run_site_research(topic, keywords, days, progress=None):
            return None

        with (
            patch.object(aggregator, "collect_async", side_effect=fake_collect_async),
            patch.object(
                aggregator, "_run_site_research", side_effect=fake_run_site_research
            ),
            patch.object(
                aggregator.summarizer,
                "create_meta_summary",
                return_value="meta",
            ),
            patch.object(
                aggregator,
                "_generate_keywords_from_providers",
                return_value={},
            ) as gen_mock,
        ):
            bundle = await aggregator.collect_and_summarize_async(
                topic="t",
                days=7,
                providers=["openai"],
                keywords=[],
            )

        gen_mock.assert_not_called()
        assert bundle.synthesized_keywords is None
