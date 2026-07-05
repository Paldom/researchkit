"""Tests for the Claude Code article-prompt generator."""

from __future__ import annotations

from pathlib import Path

from researchkit.article_prompt import build_article_prompt
from researchkit.project import ProjectConfig, UserUrlSource, create_project


def _make_project(tmp_path: Path) -> tuple[Path, ProjectConfig]:
    cfg = ProjectConfig(
        topic="AI agents for customer support",
        days=14,
        providers=["openai", "gemini"],
    )
    project = create_project(cfg, projects_dir=tmp_path / "projects")
    return project.path, cfg


class TestBuildArticlePrompt:
    def test_includes_absolute_project_path(self, tmp_path: Path) -> None:
        from researchkit.project import load_project

        path, _ = _make_project(tmp_path)
        prompt = build_article_prompt(load_project(path))
        assert str(path.resolve()) in prompt

    def test_includes_topic_and_lookback(self, tmp_path: Path) -> None:
        from researchkit.project import load_project

        path, _ = _make_project(tmp_path)
        prompt = build_article_prompt(load_project(path))
        assert "AI agents for customer support" in prompt
        assert "last 14 days" in prompt

    def test_references_required_files(self, tmp_path: Path) -> None:
        from researchkit.project import load_project

        path, _ = _make_project(tmp_path)
        prompt = build_article_prompt(load_project(path))
        for fname in ("config.json", "result.json", "report.md", "run.log"):
            assert fname in prompt

    def test_user_sources_block_present_when_dir_exists(self, tmp_path: Path) -> None:
        from researchkit.project import load_project

        path, _ = _make_project(tmp_path)
        # Materialize the user_sources/ directory so the prompt branches into
        # the "exists" form.
        project = load_project(path)
        project.add_user_url_source(UserUrlSource(url="https://ex.com"))
        # Force the dir existing (URLs alone don't create it).
        project.user_sources_dir.mkdir(parents=True, exist_ok=True)

        prompt = build_article_prompt(project)
        assert "user_sources/" in prompt
        # The "do not cite the documents" rule must be in the prompt so the
        # user's preference survives into a Claude Code session.
        assert "do not cite" in prompt.lower()

    def test_user_sources_block_indicates_empty_when_dir_missing(
        self, tmp_path: Path
    ) -> None:
        from researchkit.project import load_project

        path, _ = _make_project(tmp_path)
        prompt = build_article_prompt(load_project(path))
        assert "no user-curated sources" in prompt

    def test_lists_typical_iteration_actions(self, tmp_path: Path) -> None:
        from researchkit.project import load_project

        path, _ = _make_project(tmp_path)
        prompt = build_article_prompt(load_project(path))
        assert "Rewrite" in prompt
        assert "diff" in prompt.lower()
