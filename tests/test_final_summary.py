"""Tests for Claude-based final report summary generators."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from researchkit.final_summary import DigestGenerator
from researchkit.providers.base import ProviderResult, Source, SourceType
from researchkit.site_research.types import (
    SiteItem,
    SiteItemSummary,
    SiteResearchBundle,
)


def _make_provider_result() -> ProviderResult:
    return ProviderResult(
        provider="openai",
        model="gpt-test",
        raw_text="Full provider report content.",
        sources=[
            Source(
                url="https://example.com/report",
                title="Provider Report",
                source_type=SourceType.WEB,
            )
        ],
    )


def _make_site_research_bundle() -> SiteResearchBundle:
    return SiteResearchBundle(
        items_by_site={
            "medium": [
                SiteItem(
                    site="medium",
                    query="ai agents",
                    title="Medium article",
                    url="https://example.com/medium",
                    author_or_channel="Author Name",
                    summary=SiteItemSummary(tldr=["Useful takeaway"]),
                )
            ]
        }
    )


class TestClaudeFinalSummaryGenerator:
    def test_build_user_prompt_includes_shared_context_sections(self) -> None:
        generator = DigestGenerator(model="claude-test", max_budget=0.1)

        prompt = generator._build_user_prompt(
            meta_summary="Meta summary text.",
            individual_summaries={"openai": "Provider summary text."},
            topic="AI agents",
            days=7,
            provider_results=[_make_provider_result()],
            site_research=_make_site_research_bundle(),
        )

        assert "**Topic:** AI agents" in prompt
        assert "## Consolidated Analysis" in prompt
        assert "Meta summary text." in prompt
        assert "## Individual Provider Summaries" in prompt
        assert "Provider summary text." in prompt
        assert "## Full Provider Reports" in prompt
        assert "Full provider report content." in prompt
        assert "## Referenced Sources & Links" in prompt
        assert "Provider Report" in prompt
        assert "## Site Research (Medium, YouTube, Exa)" in prompt
        assert "Medium article" in prompt
        assert "Useful takeaway" in prompt

    def test_generate_parses_json_response(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        generator = DigestGenerator(model="claude-test", max_budget=0.1)
        seen: list[list[str]] = []

        def fake_run(*args, **kwargs) -> SimpleNamespace:
            seen.append(args[0])
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"result": "## TL;DR\nDigest content"}),
                stderr="",
            )

        monkeypatch.setattr("researchkit.final_summary.run_subprocess", fake_run)

        result = generator.generate(
            meta_summary="Meta summary",
            individual_summaries={},
            topic="AI agents",
            days=7,
        )

        assert result == "## TL;DR\nDigest content"
        # Headless writer: the repo's Stop hook must not run (and answer) here.
        cmd = seen[0]
        assert cmd[cmd.index("--settings") + 1] == '{"disableAllHooks":true}'

    def test_generate_falls_back_to_plain_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        generator = DigestGenerator(model="claude-test", max_budget=0.1)

        def fake_run(*args, **kwargs) -> SimpleNamespace:
            return SimpleNamespace(
                returncode=0,
                stdout="Plain text digest",
                stderr="",
            )

        monkeypatch.setattr("researchkit.final_summary.run_subprocess", fake_run)

        result = generator.generate(
            meta_summary="Meta summary",
            individual_summaries={},
            topic="AI agents",
            days=7,
        )

        assert result == "Plain text digest"

    @pytest.mark.parametrize(
        ("stdout", "expected"),
        [
            # A fenced file is unwrapped: line 1 must be the frontmatter.
            ("```markdown\n---\nname: x\n---\nbody\n```", "---\nname: x\n---\nbody\n"),
            # The writer declined: nothing is saved.
            ("NO-SKILL: research too thin for a skill", None),
            # Preamble before the frontmatter is not a loadable skill file.
            ("Here is the skill:\n---\nname: x\n---\n", None),
            ("", None),
        ],
    )
    def test_skill_generator_saves_only_a_real_skill_file(
        self, monkeypatch: pytest.MonkeyPatch, stdout: str, expected: str | None
    ) -> None:
        from researchkit.final_summary import SkillGenerator

        generator = SkillGenerator(model="claude-test", max_budget=0.1)
        monkeypatch.setattr(
            "researchkit.final_summary.run_subprocess",
            lambda *a, **k: SimpleNamespace(
                returncode=0, stdout=json.dumps({"result": stdout}), stderr=""
            ),
        )
        result = generator.generate(
            meta_summary="m", individual_summaries={}, topic="t", days=7
        )
        assert result == expected

    def test_article_and_skill_are_distinct_writers(self) -> None:
        from researchkit.final_summary import (
            ArticleGenerator,
            ProfessionalOverviewGenerator,
            SkillGenerator,
        )

        article = ArticleGenerator().get_system_prompt()
        assert "H1" in article
        assert article != ProfessionalOverviewGenerator().get_system_prompt()
        assert "NO-SKILL" in SkillGenerator().get_system_prompt()
        assert (ArticleGenerator.stage_name, SkillGenerator.stage_name) == (
            "article",
            "skill",
        )

    def test_generate_returns_none_on_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        generator = DigestGenerator(model="claude-test", max_budget=0.1)

        def fake_run(*args, **kwargs) -> SimpleNamespace:
            raise subprocess.TimeoutExpired(cmd="claude", timeout=300)

        monkeypatch.setattr("researchkit.final_summary.run_subprocess", fake_run)

        result = generator.generate(
            meta_summary="Meta summary",
            individual_summaries={},
            topic="AI agents",
            days=7,
        )

        assert result is None
        assert generator.last_error is not None
        assert generator.last_error.startswith("timed out after")

    def test_nonzero_exit_records_the_cli_message_not_the_json_blob(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # What a logged-out Claude Code CLI really prints: exit 1 plus the
        # JSON result envelope; the reason lives in its `result` field.
        generator = DigestGenerator(model="claude-test", max_budget=0.1)
        envelope = {
            "type": "result",
            "is_error": True,
            "duration_api_ms": 0,
            "result": "Failed to authenticate: OAuth session expired",
        }

        def fake_run(*args, **kwargs) -> SimpleNamespace:
            return SimpleNamespace(returncode=1, stdout=json.dumps(envelope), stderr="")

        monkeypatch.setattr("researchkit.final_summary.run_subprocess", fake_run)
        assert generator._run_claude("prompt") is None
        assert generator.last_error == (
            "Claude CLI exited with code 1: Failed to authenticate: OAuth session expired"
        )
        # A later success clears it.
        monkeypatch.setattr(
            "researchkit.final_summary.run_subprocess",
            lambda *a, **k: SimpleNamespace(
                returncode=0, stdout=json.dumps({"result": "# ok"}), stderr=""
            ),
        )
        assert generator._run_claude("prompt") == "# ok"
        assert generator.last_error is None

    def test_generate_returns_none_on_cli_error_payload(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        generator = DigestGenerator(model="claude-test", max_budget=0.1)

        def fake_run(*args, **kwargs) -> SimpleNamespace:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"is_error": True, "result": "failure"}),
                stderr="",
            )

        monkeypatch.setattr("researchkit.final_summary.run_subprocess", fake_run)

        result = generator.generate(
            meta_summary="Meta summary",
            individual_summaries={},
            topic="AI agents",
            days=7,
        )

        assert result is None
        assert generator.last_error == "CLI error: failure"

    def test_generate_returns_none_on_empty_response(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        generator = DigestGenerator(model="claude-test", max_budget=0.1)

        def fake_run(*args, **kwargs) -> SimpleNamespace:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"result": ""}),
                stderr="",
            )

        monkeypatch.setattr("researchkit.final_summary.run_subprocess", fake_run)

        result = generator.generate(
            meta_summary="Meta summary",
            individual_summaries={},
            topic="AI agents",
            days=7,
        )

        assert result is None
