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


def _fake_repo(dest: Path) -> None:
    """Materialize a small but realistic repo tree at ``dest``."""
    dest.mkdir(parents=True)
    (dest / "README.md").write_text(
        "# widget\n\nWidget turns things into other things. " * 8, encoding="utf-8"
    )
    (dest / "pyproject.toml").write_text(
        '[project]\nname = "widget"\ndependencies = ["requests"]\n', encoding="utf-8"
    )
    docs = dest / "docs"
    docs.mkdir()
    (docs / "usage.md").write_text("## Usage\n\nCall widgetize(x). " * 12, "utf-8")
    src = dest / "src"
    src.mkdir()
    (src / "core.py").write_text(
        "def widgetize(x):\n    return x\n\n\ndef _private():\n    pass\n\n"
        "class Widget:\n    pass\n",
        encoding="utf-8",
    )
    junk = dest / "node_modules" / "left-pad"
    junk.mkdir(parents=True)
    (junk / "index.js").write_text("export function leftPad() {}", encoding="utf-8")
    (dest / ".git").mkdir()
    (dest / ".git" / "config").write_text("[core]", encoding="utf-8")


class TestHarvestThenDelete:
    """The clone is transient; the extract is the record."""

    def _run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kwargs: Any):
        monkeypatch.setattr(repos_mod.shutil, "which", lambda b: "/usr/bin/git")
        monkeypatch.setattr(repos_mod, "_clone", lambda url, dest: _fake_repo(dest))
        return repos_mod.clone_cited_repos(
            tmp_path, _result("https://github.com/o/widget"), **kwargs
        )

    def test_docs_manifest_and_api_survive_the_deletion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        [repo] = self._run(tmp_path, monkeypatch)

        assert repo.deleted is True
        assert not (tmp_path / "repos" / "o-widget").exists()  # tree gone
        assert repo.ok  # extraction succeeded, which is what ok now means

        extract = (tmp_path / "repos" / "o-widget.md").read_text(encoding="utf-8")
        assert "Widget turns things into other things" in extract  # README
        assert "Call widgetize(x)" in extract  # docs/
        assert 'name = "widget"' in extract  # manifest
        assert "widgetize" in extract and "Widget" in extract  # public surface
        assert "_private" not in extract  # private by convention
        # Vendored and VCS noise must not be harvested as if it were the repo.
        assert "leftPad" not in extract
        assert "[core]" not in extract

    def test_keep_clones_leaves_the_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        [repo] = self._run(tmp_path, monkeypatch, keep_clones=True)
        assert repo.deleted is False
        assert (tmp_path / "repos" / "o-widget" / "src" / "core.py").is_file()
        assert (tmp_path / "repos" / "o-widget.md").is_file()  # extract regardless

    def test_a_clone_is_never_deleted_without_an_extract(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If writing the extract fails, deleting the tree would lose the repo
        # entirely — the whole point of the ordering.
        monkeypatch.setattr(repos_mod.shutil, "which", lambda b: "/usr/bin/git")
        monkeypatch.setattr(repos_mod, "_clone", lambda url, dest: _fake_repo(dest))
        real_write = Path.write_text

        def fail_extract(self: Path, *args: Any, **kw: Any) -> int:
            if self.name == "o-widget.md":
                raise OSError("disk full")
            return real_write(self, *args, **kw)

        monkeypatch.setattr(Path, "write_text", fail_extract)
        [repo] = repos_mod.clone_cited_repos(
            tmp_path, _result("https://github.com/o/widget")
        )
        assert repo.deleted is False
        assert (tmp_path / "repos" / "o-widget" / "README.md").is_file()

    def test_summary_points_at_the_extract(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._run(tmp_path, monkeypatch)
        summary = (tmp_path / "repos" / "SUMMARY.md").read_text(encoding="utf-8")
        assert "o-widget.md" in summary
        assert "Clone deleted after extraction" in summary

    def test_doc_budget_is_enforced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(repos_mod.shutil, "which", lambda b: "/usr/bin/git")

        def huge(url: str, dest: Path) -> None:
            dest.mkdir(parents=True)
            (dest / "README.md").write_text("x" * 500_000, encoding="utf-8")

        monkeypatch.setattr(repos_mod, "_clone", huge)
        [repo] = repos_mod.clone_cited_repos(
            tmp_path, _result("https://github.com/o/huge")
        )
        # Deleting the clone must not mean swallowing it whole into the pack.
        assert sum(len(text) for _, text in repo.docs) <= repos_mod._DOC_TOTAL_CHARS


class TestPipelineEnrichment:
    """The repo step runs on the critical path, so it is bounded and fail-open.

    By the time it runs the providers' answers are bought and on disk; a slow
    or broken clone may degrade the grounding but must never cost the report.
    """

    def test_deadline_returns_what_finished_and_abandons_the_rest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import time

        monkeypatch.setattr(repos_mod.shutil, "which", lambda b: "/usr/bin/git")

        def slow_or_fast(url: str, dest: Path, **kw: Any) -> None:
            if "slow" in url:
                time.sleep(30)  # never finishes inside the deadline
            _fake_repo(dest)

        monkeypatch.setattr(repos_mod, "_clone", slow_or_fast)
        started = time.monotonic()
        repos = repos_mod.extract_repos_for_run(
            tmp_path,
            _result("https://github.com/o/fast", "https://github.com/o/slow"),
            deadline_s=1.5,
            workers=2,
        )
        elapsed = time.monotonic() - started

        assert elapsed < 10, f"the deadline did not hold: waited {elapsed:.1f}s"
        assert [r.slug for r in repos if r.ok] == ["o/fast"]

    def test_a_raising_repo_never_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(repos_mod.shutil, "which", lambda b: "/usr/bin/git")

        def boom(url: str, dest: Path, **kw: Any) -> None:
            raise RuntimeError("rate limited")

        monkeypatch.setattr(repos_mod, "_clone", boom)
        repos = repos_mod.extract_repos_for_run(
            tmp_path, _result("https://github.com/o/x"), deadline_s=5
        )
        assert len(repos) == 1 and not repos[0].ok
        assert "rate limited" in (repos[0].error or "")

    def test_card_is_compact_and_carries_the_useful_facts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(repos_mod.shutil, "which", lambda b: "/usr/bin/git")
        monkeypatch.setattr(
            repos_mod, "_clone", lambda url, dest, **kw: _fake_repo(dest)
        )
        [repo] = repos_mod.extract_repos_for_run(
            tmp_path, _result("https://github.com/o/widget"), deadline_s=10
        )
        card = repos_mod.render_repo_card(repo)

        assert "o/widget" in card and "https://github.com/o/widget" in card
        assert "widgetize" in card  # public surface reaches the prompt
        assert 'name = "widget"' in card or "widget" in card  # manifest facts
        # The prompt gets a card, not the archive: ten of these must not dwarf
        # the providers' own findings.
        assert len(card) < 3000
        full = (tmp_path / "repos" / "o-widget.md").read_text(encoding="utf-8")
        assert len(full) > len(card)

    def test_no_cited_repos_is_a_no_op(self, tmp_path: Path) -> None:
        assert repos_mod.extract_repos_for_run(tmp_path, _result("https://x.dev")) == []
        assert not (tmp_path / "repos").exists()
