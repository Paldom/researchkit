"""Tests for cited-repo cloning and the --since-last update run."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import researchkit.cli as cli
import researchkit.repos as repos_mod


def _result(*urls: str) -> dict[str, Any]:
    return {"provider_results": [{"sources": [{"url": u} for u in urls]}]}


class TestRepoExtraction:
    def test_deep_links_collapse_to_their_repo(self) -> None:
        urls = repos_mod.extract_repo_urls(
            _result(
                "https://github.com/Paldom/researchkit",
                "https://github.com/Paldom/researchkit/issues/3",
                "https://github.com/Paldom/researchkit/blob/main/README.md",
            )
        )
        assert urls == ["https://github.com/Paldom/researchkit"]

    def test_non_repo_namespaces_and_hosts_are_rejected(self) -> None:
        assert (
            repos_mod.extract_repo_urls(
                _result(
                    "https://github.com/features/copilot",
                    "https://github.com/topics/python",
                    "https://github.com/sponsors/someone",
                    "https://example.com/github.com/fake/repo",
                    "https://gitlab.com/real/repo",
                )
            )
            == []
        )

    def test_git_suffix_and_trailing_slash_normalize(self) -> None:
        urls = repos_mod.extract_repo_urls(
            _result(
                "https://github.com/a/b.git",
                "https://github.com/a/b/",
                "https://www.github.com/a/b",
            )
        )
        assert urls == ["https://github.com/a/b"]

    def test_limit_is_honoured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(repos_mod.shutil, "which", lambda b: "/usr/bin/git")
        seen: list[str] = []
        monkeypatch.setattr(
            repos_mod, "_clone", lambda url, dest: (seen.append(url), dest.mkdir())[1]
        )
        repos_mod.clone_cited_repos(
            tmp_path,
            _result(*[f"https://github.com/o/r{i}" for i in range(10)]),
            limit=3,
        )
        assert len(seen) == 3


class TestCloneReporting:
    def test_failed_clone_is_recorded_not_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(repos_mod.shutil, "which", lambda b: "/usr/bin/git")

        def boom(url: str, dest: Path) -> None:
            raise RuntimeError("repository not found")

        monkeypatch.setattr(repos_mod, "_clone", boom)
        cloned = repos_mod.clone_cited_repos(
            tmp_path, _result("https://github.com/o/private")
        )
        assert len(cloned) == 1 and not cloned[0].ok
        summary = (tmp_path / "repos" / "SUMMARY.md").read_text(encoding="utf-8")
        assert "Not cloned" in summary and "repository not found" in summary

    def test_missing_git_is_a_skip_not_a_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(repos_mod.shutil, "which", lambda b: None)
        assert (
            repos_mod.clone_cited_repos(tmp_path, _result("https://github.com/o/r"))
            == []
        )

    def test_no_repos_writes_nothing(self, tmp_path: Path) -> None:
        assert (
            repos_mod.clone_cited_repos(tmp_path, _result("https://example.com")) == []
        )
        assert not (tmp_path / "repos").exists()


class TestSinceLast:
    def _project(self, tmp_path: Path, age_days: float) -> Any:
        result = tmp_path / "result.json"
        result.write_text(json.dumps({"topic": "t"}), encoding="utf-8")
        report = tmp_path / "report.md"
        report.write_text("# old report", encoding="utf-8")
        stamp = (datetime.now() - timedelta(days=age_days)).timestamp()
        import os

        os.utime(result, (stamp, stamp))
        return SimpleNamespace(
            path=tmp_path,
            result_path=result,
            report_path=report,
            has_results=True,
            config=SimpleNamespace(days=30, topic="t"),
        )

    def test_window_narrows_to_the_gap_and_archives(self, tmp_path: Path) -> None:
        project = self._project(tmp_path, age_days=3.2)
        window = cli._narrow_to_since_last(project)
        assert window == 4  # ceil of the elapsed days
        assert project.config.days == 4  # in-memory only, not persisted
        archived = list((tmp_path / "history").glob("report-*.md"))
        assert len(archived) == 1 and "old report" in archived[0].read_text()

    def test_same_day_update_still_asks_for_today(self, tmp_path: Path) -> None:
        project = self._project(tmp_path, age_days=0.01)
        assert cli._narrow_to_since_last(project) == 1

    def test_requires_a_previous_run(self, tmp_path: Path) -> None:
        project = SimpleNamespace(
            path=tmp_path,
            has_results=False,
            config=SimpleNamespace(days=30),
        )
        with pytest.raises(FileNotFoundError):
            cli._narrow_to_since_last(project)
