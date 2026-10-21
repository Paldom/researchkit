"""Tests for service-level final summary orchestration."""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import ClassVar

import pytest

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
        kimi="kimi-test",
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


class RecordingArticleGenerator:
    """Records article calls for service tests."""

    calls: ClassVar[list[dict[str, object]]] = []

    @classmethod
    def from_effective_models(cls, effective_models: EffectiveModels):
        return cls()

    def generate(self, **kwargs) -> str:
        self.__class__.calls.append(kwargs)
        return "# Article headline\n\nArticle body."


class DecliningSkillGenerator:
    """The skill writer answering NO-SKILL: nothing must be saved."""

    calls: ClassVar[list[dict[str, object]]] = []
    text: ClassVar[str | None] = None

    @classmethod
    def from_effective_models(cls, effective_models: EffectiveModels):
        return cls()

    def generate(self, **kwargs) -> str | None:
        self.__class__.calls.append(kwargs)
        return self.__class__.text


@pytest.fixture(autouse=True)
def _no_real_writers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a service test spawn the real Claude CLI writers."""
    import researchkit.service as service_module

    RecordingArticleGenerator.calls = []
    DecliningSkillGenerator.calls = []
    DecliningSkillGenerator.text = None
    monkeypatch.setattr(service_module, "ArticleGenerator", RecordingArticleGenerator)
    monkeypatch.setattr(service_module, "SkillGenerator", DecliningSkillGenerator)


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
        # PLAN 1.14 / 1.3: a monotonic per-stage timeline and the honest status.
        timings = artifacts.report_json["timings"]
        assert [t["stage"] for t in timings] == stages
        assert all(a["t_s"] <= b["t_s"] for a, b in itertools.pairwise(timings))
        assert artifacts.report_json["run_status"]["status"] in (
            "ok",
            "degraded",
            "failed",
        )
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


class TestPartialPersistenceAndResume:
    """Provider answers survive a killed run and are not bought twice."""

    def test_provider_result_round_trips_through_disk(self, tmp_path: Path) -> None:
        from researchkit.providers.base import Source, SourceType
        from researchkit.service import SocialResearchService

        svc = SocialResearchService(projects_dir=tmp_path)
        project = svc.create_project(topic="round trip", providers=["openai"])
        original = ProviderResult(
            provider="openai",
            model="gpt-test",
            raw_text="expensive text",
            sources=[
                Source(
                    url="https://x/1",
                    title="T",
                    snippet="s",
                    source_type=SourceType.WEB,
                )
            ],
            meta={"cost": 1},
        )

        project.save_partial(original.provider, original.to_dict())
        restored = ProviderResult.from_dict(project.load_partials()["openai"])

        assert restored == original  # raw_text and sources intact, not a summary

    def test_unreadable_partial_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        from researchkit.service import SocialResearchService

        svc = SocialResearchService(projects_dir=tmp_path)
        project = svc.create_project(topic="truncated", providers=["openai"])
        project.save_partial("openai", {"provider": "openai", "raw_text": "good"})
        project.partial_dir.mkdir(parents=True, exist_ok=True)
        (project.partial_dir / "gemini.json").write_text("{truncated", encoding="utf-8")

        loaded = project.load_partials()
        assert list(loaded) == ["openai"]  # the torn file is dropped, not raised

    def test_run_project_persists_partials_then_clears_them(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        import researchkit.service as service_module
        from researchkit.service import SocialResearchService

        seen: dict[str, object] = {}

        class CapturingAggregator(FakeAggregator):
            def __init__(self, *args, **kwargs) -> None:
                seen.update(kwargs)
                # Mimic the real fan-out: results land through the sink, and
                # snapshot the disk right then — that mid-run state is the whole
                # point, and it is gone by the time the test can look.
                sink = kwargs.get("partial_sink")
                if sink:
                    sink(_make_bundle().provider_results[0])
                    seen["on_disk_mid_run"] = project.load_partials()

        monkeypatch.setattr(service_module, "InsightAggregator", CapturingAggregator)
        monkeypatch.setattr(service_module, "DigestGenerator", RecordingDigestGenerator)
        monkeypatch.setattr(
            service_module,
            "ProfessionalOverviewGenerator",
            RecordingProfessionalOverviewGenerator,
        )

        svc = SocialResearchService(projects_dir=tmp_path)
        project = svc.create_project(topic="partials", providers=["openai"])
        svc.run_project(project)

        # Written the moment the provider answered — this is what survives a kill.
        mid_run = seen["on_disk_mid_run"]
        assert isinstance(mid_run, dict)
        assert mid_run["openai"]["raw_text"] == "Provider output text."
        # The finished result.json supersedes them, so partial/ is cleaned up.
        assert project.load_partials() == {}
        assert project.result_path.is_file()

    def test_run_project_writes_article_and_skill_next_to_the_report(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        import researchkit.service as service_module
        from researchkit.service import SocialResearchService

        monkeypatch.setattr(service_module, "InsightAggregator", FakeAggregator)
        monkeypatch.setattr(service_module, "DigestGenerator", RecordingDigestGenerator)
        monkeypatch.setattr(
            service_module,
            "ProfessionalOverviewGenerator",
            RecordingProfessionalOverviewGenerator,
        )
        DecliningSkillGenerator.text = "---\nname: x\n---\n\nbody\n"

        svc = SocialResearchService(projects_dir=tmp_path)
        project = svc.create_project(topic="handoff", providers=["openai"])
        artifacts = svc.run_project(project)

        # Both writers saw the same research body the overview gets, once each.
        assert (
            RecordingArticleGenerator.calls[0]["meta_summary"] == "Meta summary text."
        )
        assert len(DecliningSkillGenerator.calls) == 1
        assert artifacts.article_markdown == "# Article headline\n\nArticle body."
        assert project.article_path.read_text(encoding="utf-8").startswith("# Article")
        assert project.skill_path.read_text(encoding="utf-8").startswith("---\nname: x")
        assert project.summary()["has_article"] and project.summary()["has_skill"]

        # A declined skill leaves no file behind.
        DecliningSkillGenerator.text = None
        other = svc.create_project(topic="thin", providers=["openai"])
        svc.run_project(other)
        assert not other.skill_path.exists()
        assert other.summary()["has_skill"] is False

    def test_run_project_reuses_partials_left_by_an_interrupted_run(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        import researchkit.service as service_module
        from researchkit.service import SocialResearchService

        seen: dict[str, object] = {}

        class CapturingAggregator(FakeAggregator):
            def __init__(self, *args, **kwargs) -> None:
                seen.update(kwargs)

        monkeypatch.setattr(service_module, "InsightAggregator", CapturingAggregator)
        monkeypatch.setattr(service_module, "DigestGenerator", RecordingDigestGenerator)
        monkeypatch.setattr(
            service_module,
            "ProfessionalOverviewGenerator",
            RecordingProfessionalOverviewGenerator,
        )

        svc = SocialResearchService(projects_dir=tmp_path)
        project = svc.create_project(topic="resume", providers=["openai"])
        project.save_partial(
            "openai",
            ProviderResult(
                provider="openai", model="gpt-test", raw_text="already paid"
            ).to_dict(),
        )

        svc.run_project(project)
        preloaded = seen["preloaded_results"]
        assert isinstance(preloaded, dict)
        assert preloaded["openai"].raw_text == "already paid"

        # ...and --no-resume ignores them.
        seen.clear()
        project.save_partial(
            "openai",
            ProviderResult(
                provider="openai", model="gpt-test", raw_text="already paid"
            ).to_dict(),
        )
        svc.run_project(project, resume=False)
        assert seen["preloaded_results"] == {}


class TestProvenance:
    """Every project records where it came from, for filtering and debugging."""

    def test_origin_comes_from_the_entrypoint_label(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        import os

        from researchkit.service import SocialResearchService

        monkeypatch.setenv("RESEARCHKIT_ORIGIN", "explore")
        svc = SocialResearchService(projects_dir=tmp_path)
        project = svc.create_project(topic="tagged")

        assert project.config.origin == "explore"
        assert project.config.origin_pid == os.getpid()
        summary = project.summary()
        assert summary["origin"] == "explore"
        assert summary["origin_pid"] == os.getpid()

    def test_subprojects_inherit_the_parents_origin(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        from researchkit.project import ProjectConfig, create_subproject
        from researchkit.service import SocialResearchService

        monkeypatch.setenv("RESEARCHKIT_ORIGIN", "explore")
        svc = SocialResearchService(projects_dir=tmp_path)
        parent = svc.create_project(topic="parent")

        # A sub-query created later, even under a differently-labelled process,
        # still belongs to the run that spawned it.
        monkeypatch.setenv("RESEARCHKIT_ORIGIN", "web")
        sub = create_subproject(
            parent,
            # parent_topic is what makes it a sub-query, exactly as the boost
            # path builds it.
            ProjectConfig(topic="sub-query", parent_topic="parent"),
            1,
        )

        assert sub.config.origin == "explore"
        assert sub.summary()["is_subproject"] is True

    def test_old_projects_without_provenance_list_as_unknown(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        # 200+ projects predate this field; an origin filter must not hide them.
        from researchkit.project import ProjectConfig, find_project
        from researchkit.service import SocialResearchService

        svc = SocialResearchService(projects_dir=tmp_path)
        project = svc.create_project(topic="legacy")
        raw = json.loads(project.config_path.read_text())
        raw.pop("origin", None)
        raw.pop("origin_pid", None)
        project.config_path.write_text(json.dumps(raw))

        reloaded = find_project(project.name, tmp_path)
        assert reloaded is not None
        assert reloaded.config.origin == ""
        assert reloaded.summary()["origin"] == "unknown"
        assert ProjectConfig.from_dict(raw).origin_pid == 0

    def test_an_explicit_batch_label_survives(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        from researchkit.service import SocialResearchService

        monkeypatch.setenv("RESEARCHKIT_ORIGIN", "nightly-cron")
        svc = SocialResearchService(projects_dir=tmp_path)
        assert svc.create_project(topic="batch").config.origin == "nightly-cron"


class TestArchiveFirst:
    """PLAN 2.4: cited sources are archived before the writers run."""

    def test_archive_runs_before_writers_and_is_recorded(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        import researchkit.service as service_module

        order: list[str] = []
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

        def archive(payload: dict) -> dict:
            order.append("archive")
            assert payload["provider_results"]  # the in-memory result, no result.json
            return {"fetched": 2, "total_cited": 3}

        events: list[dict[str, object]] = []
        artifacts = service.run(
            ResearchRequest(topic="AI agents", providers=["openai"]),
            progress=lambda e: (events.append(e), order.append(str(e["stage"]))),
            log_dir=tmp_path,
            archive=archive,
        )
        assert order.index("archive") < order.index("digest")
        assert artifacts.report_json["materials"] == {"fetched": 2, "total_cited": 3}

    def test_nothing_archived_fails_the_run(self) -> None:
        from researchkit.providers.base import ProviderResult, Source
        from researchkit.service import run_status

        r = ProviderResult("grok", "m", "t", sources=[Source(url="https://a.b")])
        st = run_status([r], "article", {"fetched": 0, "total_cited": 5})
        assert st["status"] == "failed"
        assert "no cited source could be archived" in st["reasons"]
