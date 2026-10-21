"""Tests for service-level final summary orchestration."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

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

        svc = SocialResearchService(projects_dir=tmp_path)
        # Pin slots: a local models.yaml harness pin would mark the partial stale.
        monkeypatch.setattr(
            svc,
            "get_effective_models",
            lambda preset_name=None: _make_effective_models(),
        )
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
        assert order.index("archive") < order.index("report_body")
        assert artifacts.report_json["materials"] == {"fetched": 2, "total_cited": 3}
        # PLAN 1.14 / 1.3: a monotonic per-stage timeline and the honest status.
        timings = artifacts.report_json["timings"]
        assert [t["stage"] for t in timings] == [e["stage"] for e in events]
        assert all(a["t_s"] <= b["t_s"] for a, b in itertools.pairwise(timings))
        assert artifacts.report_json["run_status"]["status"] in (
            "ok",
            "degraded",
            "failed",
        )

    def test_repo_harvest_runs_beside_the_archive(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        import threading

        import researchkit.service as service_module

        monkeypatch.setattr(service_module, "InsightAggregator", FakeAggregator)
        service = SocialResearchService(projects_dir=tmp_path)
        monkeypatch.setattr(
            service,
            "get_effective_models",
            lambda preset_name=None: _make_effective_models(),
        )
        harvesting = threading.Event()

        def enrich(
            repo_dir: Path, bundle: object, emit: object, keep_clones: bool = False
        ) -> list[str]:
            harvesting.set()
            return ["repo: o/r"]

        def archive(payload: dict) -> dict:
            # Serial code would deadlock here (harvest runs after archive).
            assert harvesting.wait(5), "repo harvest did not overlap the archive"
            return {"fetched": 1, "total_cited": 1}

        monkeypatch.setattr(service, "_enrich_with_repos", enrich)
        cards: list[list[str]] = []
        real_publish = service_module.publish
        monkeypatch.setattr(
            service_module,
            "publish",
            lambda **kw: (cards.append(kw["repo_cards"]), real_publish(**kw))[1],
        )
        service.run(
            ResearchRequest(topic="AI agents", providers=["openai"]),
            log_dir=tmp_path,
            archive=archive,
            repo_dir=tmp_path,
        )
        assert cards == [["repo: o/r"]]

    def test_nothing_archived_fails_the_run(self) -> None:
        from researchkit.providers.base import ProviderResult, Source
        from researchkit.service import run_status

        r = ProviderResult("grok", "m", "t", sources=[Source(url="https://a.b")])
        st = run_status([r], "article", {"fetched": 0, "total_cited": 5})
        assert st["status"] == "failed"
        assert "no cited source could be archived" in st["reasons"]


class TestOwnBrain:
    """PLAN 3.5: the service builds the pack's brain once, in one place."""

    def _run(self, monkeypatch, tmp_path: Path, **kwargs: object) -> list[str]:
        import researchkit.brain as brain_module
        import researchkit.service as service_module

        built: list[str] = []
        monkeypatch.setattr(service_module, "InsightAggregator", FakeAggregator)
        monkeypatch.setattr(
            brain_module, "build_brain", lambda project: built.append(project.name)
        )
        monkeypatch.setattr(
            service_module,
            "run_status",
            lambda *a: {"status": "ok", "reasons": [], "slots": {}, "with_sources": 1},
        )
        svc = SocialResearchService(projects_dir=tmp_path)
        project = svc.create_project(topic="brain", providers=["openai"])
        svc.run_project(project, **kwargs)
        return built

    def test_run_builds_its_own_brain(self, monkeypatch, tmp_path: Path) -> None:
        assert len(self._run(monkeypatch, tmp_path)) == 1

    def test_sub_projects_and_no_materials_skip_it(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        assert self._run(monkeypatch, tmp_path, own_brain=False) == []
        assert self._run(monkeypatch, tmp_path, materials=False) == []


def test_council_pre_pass_without_fan_out(monkeypatch, tmp_path: Path) -> None:
    """PLAN 5.4 ablation arm: decompose=False keeps the council, runs once."""
    import researchkit.council as council_module
    from researchkit.council import CouncilResult

    class FakeCouncil:
        @classmethod
        def from_effective_models(cls, em: object) -> FakeCouncil:
            return cls()

        def deliberate(self, topic: str, count: int = 10) -> CouncilResult:
            return CouncilResult(
                improved_topic="better topic",
                keywords=["k"],
                decompose=True,
                subqueries=["a sub-question", "another sub-question"],
            )

    monkeypatch.setattr(council_module, "LLMCouncil", FakeCouncil)
    svc = SocialResearchService(projects_dir=tmp_path)
    monkeypatch.setattr(
        svc, "get_effective_models", lambda preset_name=None: _make_effective_models()
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        svc,
        "create_and_run_project",
        lambda **kw: (calls.append(kw), (object(), object()))[1],
    )
    result = svc.create_and_run_boosted(
        "topic", providers=["hackernews"], force_boost=True, decompose=False
    )
    assert result.decomposed is False
    assert [c["topic"] for c in calls] == ["better topic"]
