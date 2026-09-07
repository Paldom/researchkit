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


def test_report_carries_no_pipeline_metadata() -> None:
    # The report is about the topic: no model ids, preset fingerprints,
    # per-provider keyword lists or status tables. Those stay in result.json.
    bundle = _make_bundle("Overview text.")
    bundle.provider_keywords = {"openai": ["kw1", "kw2"]}
    bundle.provider_results.append(
        ProviderResult(provider="glm", model="glm-test", raw_text="", error="boom")
    )
    lean = format_as_markdown(bundle, include_raw=False)
    assert "gpt-test" not in lean
    assert "Provider keyword contributions" not in lean
    assert "**Providers:**" not in lean
    assert "## Analytics" not in lean
    assert "## Provider Summaries" not in lean  # intermediate, raw-only
    assert (
        "**Coverage:** 1/2 providers succeeded, 0 sources cited (failed: glm)" in lean
    )

    full = format_as_markdown(bundle, include_raw=True)
    assert "## Provider Summaries" in full
    assert "### ✓ Openai" in full
    assert "gpt-test" not in full


def test_raw_provider_headings_are_demoted() -> None:
    # Providers emit `## Sources` sections (instructed); embedded raw text
    # must not create top-level report sections — brainkit chunks report.md
    # on `## ` lines and would turn each into a junk note.
    bundle = _make_bundle(None)
    bundle.provider_results[0].raw_text = (
        "Analysis prose.\n\n## Sources\n- [x](https://x.test)\n"
        "```\n## inside a fence stays\n```\n"
    )
    report = format_as_markdown(bundle, include_raw=True)
    assert "\n#### Sources\n" in report
    assert "\n## Sources\n" not in report
    assert "## inside a fence stays" in report  # fence content untouched
