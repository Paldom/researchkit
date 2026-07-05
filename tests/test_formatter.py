"""Tests for report formatting."""

from __future__ import annotations

from researchkit.aggregator import InsightBundle
from researchkit.formatter import format_as_markdown
from researchkit.providers.base import ProviderResult


def _make_bundle(professional_overview: str | None) -> InsightBundle:
    return InsightBundle(
        topic="AI agents",
        keywords=[],
        days=7,
        providers_queried=["openai"],
        meta_summary="Consolidated analysis text.",
        provider_results=[
            ProviderResult(
                provider="openai",
                model="gpt-test",
                raw_text="Provider output",
            )
        ],
        individual_summaries={"openai": "- Summary bullet"},
        professional_overview_markdown=professional_overview,
    )


class TestFormatter:
    def test_professional_overview_renders_before_digest(self) -> None:
        markdown = format_as_markdown(
            _make_bundle("Overview text."),
            include_raw=False,
            digest_markdown="Digest text.",
        )

        assert "## Professional Overview" in markdown
        assert "Overview text." in markdown
        assert markdown.index("## Professional Overview") < markdown.index("## Digest")
        assert markdown.index("## Digest") < markdown.index("## Consolidated Analysis")

    def test_professional_overview_section_is_omitted_when_absent(self) -> None:
        markdown = format_as_markdown(
            _make_bundle(None),
            include_raw=False,
            digest_markdown="Digest text.",
        )

        assert "## Professional Overview" not in markdown
        assert "## Digest" in markdown
