"""Tests for service-level final summary orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from researchkit.aggregator import InsightBundle
from researchkit.providers.base import ProviderResult
from researchkit.service import ResearchRequest, SocialResearchService
from researchkit.system_config import EffectiveModels


def _make_effective_models() -> EffectiveModels:
    return EffectiveModels(
        openai="gpt-test",
        gemini="gemini-test",
        grok="grok-test",
        perplexity="perplexity-test",
        tavily="tavily-test",
        claude="claude-test",
        github="github-test",
        glm="glm-test",
        summarizer="summary-test",
        site_summarizer="site-summary-test",
        improver="improver-test",
        reasoning_effort="medium",
        perplexity_search_type="pro",
        tavily_search_depth="advanced",
        claude_max_budget=2.0,
        preset_name="test",
    )


def _make_bundle() -> InsightBundle:
    return InsightBundle(
        topic="AI agents",
        keywords=[],
        days=7,
        providers_queried=["openai"],
        meta_summary="Meta summary text.",
        provider_results=[
            ProviderResult(
                provider="openai",
                model="gpt-test",
                raw_text="Provider output text.",
            )
        ],
        individual_summaries={"openai": "Provider summary text."},
    )


class FakeAggregator:
    """Test double for the research aggregator."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def collect_and_summarize_sync(self, *args, **kwargs) -> InsightBundle:
        return _make_bundle()


class RecordingDigestGenerator:
    """Records digest calls for service tests."""

    calls: ClassVar[list[dict[str, object]]] = []

    @classmethod
    def from_effective_models(
        cls, effective_models: EffectiveModels
    ) -> RecordingDigestGenerator:
        return cls()

    def generate(self, **kwargs) -> str:
        self.__class__.calls.append(kwargs)
        return "Digest markdown"


class RecordingProfessionalOverviewGenerator:
    """Records professional overview calls for service tests."""

    calls: ClassVar[list[dict[str, object]]] = []

    @classmethod
    def from_effective_models(
        cls,
        effective_models: EffectiveModels,
    ) -> RecordingProfessionalOverviewGenerator:
        return cls()

    def generate(self, **kwargs) -> str:
        self.__class__.calls.append(kwargs)
        return "Professional overview markdown"


class FailingProfessionalOverviewGenerator:
    """Raises during generation to test failure isolation."""

    @classmethod
    def from_effective_models(
        cls,
        effective_models: EffectiveModels,
    ) -> FailingProfessionalOverviewGenerator:
        return cls()

    def generate(self, **kwargs) -> str:
        raise RuntimeError("overview failed")


class TestSocialResearchService:
    def test_run_generates_both_final_summaries_and_serializes_overview(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        RecordingDigestGenerator.calls = []
        RecordingProfessionalOverviewGenerator.calls = []

        import researchkit.service as service_module

        monkeypatch.setattr(service_module, "InsightAggregator", FakeAggregator)
        monkeypatch.setattr(service_module, "DigestGenerator", RecordingDigestGenerator)
        monkeypatch.setattr(
            service_module,
            "ProfessionalOverviewGenerator",
            RecordingProfessionalOverviewGenerator,
        )

        service = SocialResearchService(projects_dir=tmp_path)
        monkeypatch.setattr(
            service,
            "get_effective_models",
            lambda preset_name=None: _make_effective_models(),
        )

        events: list[dict[str, object]] = []
        artifacts = service.run(
            ResearchRequest(topic="AI agents", providers=["openai"]),
            save=False,
            progress=events.append,
            log_dir=tmp_path,
        )

        assert len(RecordingDigestGenerator.calls) == 1
        assert len(RecordingProfessionalOverviewGenerator.calls) == 1
        assert RecordingDigestGenerator.calls[0]["meta_summary"] == "Meta summary text."
        assert (
            RecordingProfessionalOverviewGenerator.calls[0]["meta_summary"]
            == "Meta summary text."
        )
        assert (
            artifacts.report_json["professional_overview_markdown"]
            == "Professional overview markdown"
        )
        assert "## Professional Overview" in artifacts.report_markdown
        assert "## Digest" in artifacts.report_markdown

        stages = [event["stage"] for event in events]
        assert "digest" in stages
        assert "digest_done" in stages
        assert "professional_overview" in stages
        assert "professional_overview_done" in stages

    def test_run_keeps_digest_when_professional_overview_fails(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        import researchkit.service as service_module

        monkeypatch.setattr(service_module, "InsightAggregator", FakeAggregator)
        monkeypatch.setattr(service_module, "DigestGenerator", RecordingDigestGenerator)
        monkeypatch.setattr(
            service_module,
            "ProfessionalOverviewGenerator",
            FailingProfessionalOverviewGenerator,
        )

        service = SocialResearchService(projects_dir=tmp_path)
        monkeypatch.setattr(
            service,
            "get_effective_models",
            lambda preset_name=None: _make_effective_models(),
        )

        events: list[dict[str, object]] = []
        artifacts = service.run(
            ResearchRequest(topic="AI agents", providers=["openai"]),
            save=False,
            progress=events.append,
            log_dir=tmp_path,
        )

        assert "## Digest" in artifacts.report_markdown
        assert "## Professional Overview" not in artifacts.report_markdown
        assert "professional_overview_markdown" not in artifacts.report_json

        stage_to_message = {event["stage"]: event["message"] for event in events}
        assert stage_to_message["digest_done"] == "Digest generated"
        assert (
            stage_to_message["professional_overview_done"]
            == "Professional overview skipped"
        )
