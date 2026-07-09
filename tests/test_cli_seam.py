"""Tests for the CLI research→brain seam fixes (FB #1, #4, #9, #10, #11)."""

from __future__ import annotations

import argparse
import sys
import types
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

import researchkit.cli as cli
from researchkit.project import Project, ProjectConfig
from researchkit.service import BoostedArtifacts


def _project(tmp_path: Path, name: str, with_result: bool = True) -> Project:
    folder = tmp_path / name
    folder.mkdir(parents=True, exist_ok=True)
    if with_result:
        (folder / "result.json").write_text("{}", encoding="utf-8")
    return Project(
        path=folder,
        config=ProjectConfig(topic=name),
        created_at=datetime(2026, 7, 9),
    )


def _boost_args(**overrides: Any) -> argparse.Namespace:
    defaults: dict[str, Any] = {
        "verbose": False,
        "materials": False,
        "materials_limit": 25,
        "ingest": None,
        "log_level": "INFO",
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class FakeBoostService:
    def __init__(self, result: BoostedArtifacts) -> None:
        self.result = result

    def create_and_run_boosted(self, **kwargs: Any) -> BoostedArtifacts:
        self.kwargs = kwargs
        return self.result


class TestBoostMaterials:
    def test_materials_downloaded_per_subproject(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parent = _project(tmp_path, "parent")
        subs = [
            _project(tmp_path / "parent" / "subprojects", f"sub_{i}") for i in (1, 2)
        ]
        unrun = _project(
            tmp_path / "parent" / "subprojects", "sub_3", with_result=False
        )
        result = BoostedArtifacts(
            project=parent,
            council_result=types.SimpleNamespace(improved_topic="t"),
            decomposed=True,
            sub_projects=[*subs, unrun],
            sub_artifacts=[],
            super_summary_markdown="# Super",
        )
        downloaded: list[tuple[str, int]] = []
        monkeypatch.setattr(
            cli,
            "_download_materials_for",
            lambda p, limit=25, refresh=False: downloaded.append((p.name, limit)) or 0,
        )
        args = _boost_args(materials=True, materials_limit=7)
        assert cli._cmd_instant_boosted(args, FakeBoostService(result), "t") == 0
        # one download per RUN sub-project, honoring --materials-limit;
        # the un-run sub and the parent (no web citations) are skipped
        assert downloaded == [("sub_1", 7), ("sub_2", 7)]

    def test_non_decomposed_run_downloads_materials(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _project(tmp_path, "single")
        result = BoostedArtifacts(
            project=project,
            council_result=types.SimpleNamespace(improved_topic="t"),
            decomposed=False,
            single_artifacts=types.SimpleNamespace(report_markdown="# R"),
        )
        downloaded: list[str] = []
        monkeypatch.setattr(
            cli,
            "_download_materials_for",
            lambda p, limit=25, refresh=False: downloaded.append(p.name) or 0,
        )
        monkeypatch.setattr(cli, "_print_article_prompt_hint", lambda p: None)
        args = _boost_args(materials=True)
        assert cli._cmd_instant_boosted(args, FakeBoostService(result), "t") == 0
        assert downloaded == ["single"]


class TestBoostHeartbeat:
    def test_heartbeat_prints_without_verbose(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        progress = cli._make_boost_progress(_boost_args())
        progress({"stage": "council_start", "message": "Convening council (a, b)"})
        progress(
            {
                "stage": "provider_done",
                "subproject": "memory systems",
                "provider": "openai",
                "ok": True,
                "done": 2,
                "total": 8,
            }
        )
        progress({"stage": "done", "subproject": "memory systems", "message": "done"})
        progress({"stage": "provider_start", "subproject": "x"})  # not a heartbeat
        err = capsys.readouterr().err
        assert "[boost] Convening council" in err
        assert "[boost] memory systems: openai OK (2/8 providers)" in err
        assert "[boost] memory systems: done" in err
        assert "provider_start" not in err

    def test_verbose_mode_does_not_double_print(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        progress = cli._make_boost_progress(_boost_args(verbose=True))
        progress({"stage": "collecting", "message": "Collecting"})
        err = capsys.readouterr().err
        assert err.count("Collecting") == 1
        assert "[boost]" not in err

    def test_verbose_still_sees_boost_lifecycle_stages(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # make_progress_callback knows nothing of council/boost stages, so
        # the heartbeat must print them even in verbose mode (red-team F4).
        progress = cli._make_boost_progress(_boost_args(verbose=True))
        progress({"stage": "council_done", "message": "Council done: 5 sub-queries"})
        err = capsys.readouterr().err
        assert err.count("Council done") == 1
        assert "[boost]" in err

    def test_parent_run_events_heartbeat_when_not_decomposed(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A non-decomposed boosted run emits events without "subproject";
        # they must still produce liveness output (red-team F5).
        progress = cli._make_boost_progress(_boost_args())
        progress(
            {
                "stage": "provider_done",
                "provider": "openai",
                "ok": True,
                "done": 2,
                "total": 4,
            }
        )
        progress({"stage": "done", "message": "Run complete: 4 succeeded"})
        err = capsys.readouterr().err
        assert "[boost] openai OK (2/4 providers)" in err
        assert "[boost] Run complete: 4 succeeded" in err


class TestProjectsDirEnvVar:
    def test_env_var_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RESEARCHKIT_PROJECTS_DIR", "/tmp/rk-out")
        assert cli._default_projects_dir() == Path("/tmp/rk-out")
        monkeypatch.delenv("RESEARCHKIT_PROJECTS_DIR")
        assert cli._default_projects_dir() == Path("projects")


class TestIngestHandoff:
    def test_ingest_uses_brainkit_when_importable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _project(tmp_path, "runme")
        calls: list[tuple[Path, Path]] = []

        def fake_ingest(project_dir: Path, brain_dir: Path) -> Any:
            calls.append((project_dir, brain_dir))
            return types.SimpleNamespace(source_notes=[1, 2], sub_reports=[])

        fake_brain = types.ModuleType("brainkit.brain")
        fake_brain.ingest_research_project = fake_ingest  # type: ignore[attr-defined]
        fake_pkg = types.ModuleType("brainkit")
        monkeypatch.setitem(sys.modules, "brainkit", fake_pkg)
        monkeypatch.setitem(sys.modules, "brainkit.brain", fake_brain)

        assert cli._ingest_into_brain(project, str(tmp_path / "brain")) == 0
        assert calls and calls[0][0].is_absolute()

    def test_ingest_without_brainkit_prints_manual_command(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setitem(sys.modules, "brainkit", None)  # force ImportError
        monkeypatch.setitem(sys.modules, "brainkit.brain", None)
        project = _project(tmp_path, "runme")
        assert cli._ingest_into_brain(project, "brain") == 1
        err = capsys.readouterr().err
        assert "brainkit ingest" in err
        assert str(project.path.resolve()) in err


class TestMaterialsLimitHint:
    def test_hint_printed_when_limit_truncates(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import researchkit.materials as materials_mod

        manifest = {
            "fetched": 2,
            "total_cited": 5,
            "entries": [
                {"status": "fetched"},
                {"status": "fetched"},
                {"status": "skipped_limit"},
                {"status": "skipped_limit"},
                {"status": "skipped_limit"},
            ],
        }
        monkeypatch.setattr(
            materials_mod, "download_materials", lambda project, **kw: manifest
        )
        project = _project(tmp_path, "capped")
        assert cli._download_materials_for(project, limit=2) == 0
        err = capsys.readouterr().err
        assert "2/5 sources archived" in err
        assert "3 skipped_limit" in err
        assert "--materials-limit 0 to fetch all" in err


class TestInstantParserFlags:
    def test_research_args_accept_new_flags(self) -> None:
        parser = argparse.ArgumentParser()
        cli._add_research_args(parser)
        args = parser.parse_args(
            ["--materials", "--materials-limit", "0", "--ingest", "/tmp/brain"]
        )
        assert args.materials and args.materials_limit == 0
        assert args.ingest == "/tmp/brain"
