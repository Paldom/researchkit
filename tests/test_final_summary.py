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

        def fake_run(*args, **kwargs) -> SimpleNamespace:
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
