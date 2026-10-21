"""The archive-first publication stage (PLAN 2.4-2.11)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from researchkit.citations import check_publication, extract_claims, renumber
from researchkit.evidence import (
    Evidence,
    EvidenceSource,
    load_evidence,
    plan_sections,
    select_passages,
)


def _project(tmp_path: Path) -> Path:
    mats = tmp_path / "materials"
    mats.mkdir()
    pages = {
        "001-a.md": (
            "https://a.example/cache",
            "Codex web search cache",
            "Codex caches web search results by default. " * 20
            + "Live mode costs 3x more.",
        ),
        "002-b.md": (
            "https://b.example/gemini",
            "Gemini grounding",
            "Gemini grounding returns segment offsets for each citation. " * 15,
        ),
    }
    entries = []
    for name, (url, title, body) in pages.items():
        (mats / name).write_text(
            f"---\nurl: {url}\ntitle: {title}\ncontent_digest: abc{name[:3]}\n---\n\n{body}\n"
        )
        entries.append(
            {
                "url": url,
                "title": title,
                "status": "fetched",
                "file": name,
                "origin": "http",
            }
        )
    entries.append(
        {"url": "https://x.com/u/status/1", "title": "A post", "status": "tos-blocked"}
    )
    (mats / "index.json").write_text(json.dumps({"entries": entries}))
    return tmp_path


class TestEvidence:
    def test_sources_numbered_and_passages_loaded(self, tmp_path: Path) -> None:
        ev = load_evidence(_project(tmp_path), {})
        assert [s.n for s in ev.sources] == [1, 2, 3]
        assert ev.sources[2].status == "tos-blocked" and not ev.sources[2].archived
        assert {p.n for p in ev.passages} == {1, 2}  # the X post has no text
        assert ev.sources[0].content_hash == "abc001"

    def test_summary_origin_is_not_evidence(self, tmp_path: Path) -> None:
        proj = _project(tmp_path)
        index = json.loads((proj / "materials" / "index.json").read_text())
        index["entries"][1]["origin"] = "summary"
        (proj / "materials" / "index.json").write_text(json.dumps(index))
        ev = load_evidence(proj, {})
        assert {p.n for p in ev.passages} == {1}

    def test_sections_from_analysis_headings(self) -> None:
        meta = "## TL;DR\nx\n## Codex caching\nCodex caches.\n## Gemini grounding\nOffsets.\n## Sources\n- a"
        assert [h for h, _ in plan_sections(meta)] == [
            "Codex caching",
            "Gemini grounding",
        ]
        assert plan_sections("no headings at all")[0][0] == "Findings"

    def test_passages_go_to_the_matching_section(self, tmp_path: Path) -> None:
        ev = load_evidence(_project(tmp_path), {})
        picks = select_passages(
            [
                ("Codex cache", "web search cache"),
                ("Gemini grounding", "segment offsets"),
            ],
            ev,
        )
        assert {p.n for p in picks["Codex cache"]} == {1}
        assert {p.n for p in picks["Gemini grounding"]} == {2}


def _ev() -> Evidence:
    return Evidence(
        sources=[
            EvidenceSource(1, "https://a.example/one", "One", "fetched", "1.md"),
            EvidenceSource(2, "https://b.example/two", "Two", "fetched", "2.md"),
            EvidenceSource(3, "https://x.com/p", "Post", "tos-blocked"),
        ]
    )


class TestCitations:
    def test_renumber_by_first_appearance_and_drop_unused(self) -> None:
        cited = renumber(
            ["Lead cites two [2].", "Body cites one [1] and two [2][1]."], _ev()
        )
        assert cited.texts == [
            "Lead cites two [1].",
            "Body cites one [2] and two [1][2].",
        ]
        assert [s.url for s in cited.sources] == [
            "https://b.example/two",
            "https://a.example/one",
        ]
        assert not cited.issues

    def test_inline_urls_become_anchors_and_unknown_ones_fail(self) -> None:
        cited = renumber(
            [
                "See [docs](https://a.example/one?utm_source=x) and https://evil.example/z."
            ],
            _ev(),
        )
        assert "docs [1]" in cited.texts[0] and "evil" not in cited.texts[0]
        assert any(i.hard and i.kind == "off_list_url" for i in cited.issues)

    def test_unknown_number_is_a_hard_issue(self) -> None:
        cited = renumber(["Made up [9]."], _ev())
        assert any(i.kind == "unknown_citation" and i.hard for i in cited.issues)

    def test_gate(self) -> None:
        text = (
            "## Finding\n"
            "Certainly! Here's the report.\n"
            "It is corroborated by one page [1].\n"
            "Call a claim corroborated only with two sources [2].\n"
            "Usage rose 47% in 2026 [2].\n"
            "A post claims more [3]."
        )
        cited = renumber([text], _ev())
        archive = {
            "https://a.example/one": "one",
            "https://b.example/two": "rose 47% in 2026",
        }
        issues = check_publication({"report.md": cited.texts[0]}, cited, archive)
        kinds = {(i.kind, i.hard) for i in issues}
        assert ("chatbot_artifact", True) in kinds
        assert ("single_origin_corroboration", True) in kinds
        assert ("unarchived_citation", False) in kinds
        assert (
            "number_not_in_archive",
            False,
        ) not in kinds  # 47 and 2026 are on the page

    def test_claims_rows(self) -> None:
        cited = renumber(["## Cache\nCodex may cache results [1][2]."], _ev())
        rows = extract_claims({"report.md": cited.texts[0]}, cited, "run-1")
        assert len(rows) == 2 and rows[0]["section"] == "Cache"
        assert rows[0]["sentence"] == "Codex may cache results."
        assert "may" in rows[0]["hedges"] and rows[0]["co_cited"]
        assert rows[0]["id"].startswith("sha256:")


class TestPublish:
    def _bundle(self) -> Any:
        from researchkit.aggregator import InsightBundle
        from researchkit.providers.base import ProviderResult, Source

        return InsightBundle(
            topic="web search citations",
            days=7,
            keywords=[],
            providers_queried=["grok"],
            provider_results=[
                ProviderResult(
                    "grok",
                    "grokcli:grok-4.7",
                    "text",
                    sources=[Source(url="https://a.example/cache")],
                )
            ],
            meta_summary="## Codex caching\nCodex caches web search.\n## Gemini grounding\nOffsets.",
            individual_summaries={"grok": "- **Cache:** yes"},
        )

    def test_end_to_end_with_fake_writers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import researchkit.publish as pub

        calls: list[str] = []

        def fake_write(self: Any, prompt: str) -> str:
            calls.append(self.stage_name)
            if self.stage_name == "report_body":
                assert (
                    "Codex caches web search results by default" in prompt
                )  # passages fed
                return (
                    "## Codex caches web search results by default\n"
                    "Codex caches results [1]; live mode costs more [1].\n\n"
                    "## Gemini returns per-segment offsets\nGemini returns offsets [2].\n\n"
                    "## What the sources did not answer\n- Pricing for Kimi."
                )
            if self.stage_name == "report_lead":
                return "## TL;DR\nCodex caches [1] and Gemini returns offsets [2].\n\n## In brief\n- **Cache**: yes [1]."
            return "# Citations differ\n\nSee [the docs](https://a.example/cache) and [bad](https://nope.example)."

        monkeypatch.setattr(pub._Writer, "write", fake_write)
        result = pub.publish(
            topic="web search citations",
            days=7,
            bundle=self._bundle(),
            project_dir=_project(tmp_path),
            effective_models=None,
            run_id="r1",
        )
        assert calls[0] == "report_body" and set(calls[1:]) == {
            "report_lead",
            "article",
        }
        md = result.report_markdown
        assert (
            md.index("## TL;DR") < md.index("## In brief") < md.index("## Codex caches")
        )
        assert "## Sources" in md and "(evidence.md)" in md
        assert "1. [Codex web search cache](https://a.example/cache)" in md
        assert any(i.kind == "article_off_list_url" for i in result.hard_issues)
        assert result.claims and result.summary()["sources_cited"] == 2

    def test_writer_failures_degrade_honestly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import researchkit.publish as pub

        monkeypatch.setattr(pub._Writer, "write", lambda self, prompt: None)
        result = pub.publish(
            topic="t",
            days=7,
            bundle=self._bundle(),
            project_dir=_project(tmp_path),
            effective_models=None,
            run_id="r1",
        )
        assert result.body is None and result.article_markdown is None
        assert "report writer failed" in result.report_markdown
        assert set(result.errors) == {"report_body", "report_lead", "article"}

    def test_failure_article_is_stdlib(self) -> None:
        from researchkit.publish import render_failure_article

        text = render_failure_article(
            "t",
            {"reasons": ["no provider returned sources"], "slots": {"kimi": "quota"}},
        )
        assert text.startswith("# Research on") and "kimi: quota" in text


def test_republish_rewrites_from_frozen_research(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`researchkit republish`: same research, new publication, result.json last."""
    import researchkit.publish as pub
    from researchkit.project import ProjectConfig, create_project
    from researchkit.service import SocialResearchService

    project = create_project(ProjectConfig(topic="web search citations"), tmp_path)
    _project(project.path)
    project.save_results(
        {
            "topic": "web search citations",
            "days": 7,
            "meta_summary": "## Codex caching\nCodex caches.",
            "provider_results": [
                {
                    "provider": "grok",
                    "model": "grokcli:grok-4.7",
                    "raw_text": "x",
                    "sources": [{"url": "https://a.example/cache"}],
                }
            ],
        },
        "# old stacked report",
    )
    monkeypatch.setattr(
        pub._Writer,
        "write",
        lambda self, prompt: (
            "## Codex caches results\nIt caches [1]."
            if self.stage_name == "report_body"
            else (
                "## TL;DR\nCaches [1].\n\n## In brief\n- **Cache**: yes [1]."
                if self.stage_name == "report_lead"
                else "# A\n\nText."
            )
        ),
    )
    svc = SocialResearchService(projects_dir=tmp_path)
    artifacts = svc.republish(project)
    assert project.report_path.read_text().startswith("# web search citations")
    assert project.claims_path.is_file() and project.evidence_path.is_file()
    saved = json.loads(project.result_path.read_text())
    assert saved["publication"]["version"] == 2 and saved["citations"][0]["n"] == 1
    assert artifacts.status in ("ok", "degraded")


def test_cosmetic_duplicates_share_one_number() -> None:
    ev = Evidence(
        sources=[
            EvidenceSource(1, "https://idrc.example/brief", "Brief", "fetched", "1.md"),
            EvidenceSource(
                2, "https://www.idrc.example/brief/", "Brief", "fetched", "2.md"
            ),
        ]
    )
    cited = renumber(["A [1] and B [2]."], ev)
    assert cited.texts == ["A [1] and B [1]."] and len(cited.sources) == 1


def test_source_titles_are_cleaned() -> None:
    from researchkit.publish import _clean_title

    assert _clean_title("[[arxiv.org]", "https://arxiv.org/abs/1") == "arxiv.org"
    assert _clean_title("x" * 200, "https://x.com/u/status/9") == "x.com/u/status/9"
    assert _clean_title("Good title", "https://a.b") == "Good title"


def test_skill_is_made_on_request_with_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PLAN 2.14: no skill per run; `researchkit skill` feeds the claims."""
    from researchkit.final_summary import SkillGenerator
    from researchkit.project import ProjectConfig, create_project
    from researchkit.service import SocialResearchService

    project = create_project(ProjectConfig(topic="t"), tmp_path)
    project.save_results({"topic": "t", "days": 7, "meta_summary": "m"}, "# r")
    project.claims_path.write_text(
        json.dumps({"sentence": "Codex may cache.", "canonical_url": "https://a.b"})
        + "\n"
    )
    seen: dict[str, Any] = {}

    def fake_generate(self: Any, **kw: Any) -> str:
        seen.update(kw)
        return "---\nname: t\n---\nskill"

    monkeypatch.setattr(SkillGenerator, "generate", fake_generate)
    text = SocialResearchService(projects_dir=tmp_path).make_skill(project)
    assert text and project.skill_path.read_text().startswith("---")
    assert "Codex may cache. (https://a.b)" in seen["meta_summary"]


def test_citations_after_the_full_stop_belong_to_the_sentence_before() -> None:
    """Live run 2026-10-21: '…2026. [14] Next…' shifted every claim one source."""
    from researchkit.citations import _sentences

    text = (
        "Kimi's search retired on 20 October 2026. [14] Codex scored 24/24 [9]. "
        "Grok streams messages. [3][5]"
    )
    assert _sentences(text) == [
        ("", "Kimi's search retired on 20 October 2026. [14]"),
        ("", "Codex scored 24/24 [9]."),
        ("", "Grok streams messages. [3][5]"),
    ]


def test_meta_talk_is_an_advisory_and_bare_anchors_make_no_claim() -> None:
    from researchkit.citations import CitedDocuments

    cited = CitedDocuments(texts=[], sources=[EvidenceSource(n=1, url="https://a.io")])
    text = "Providers found a few posts [1]. Codex caches results [1]."
    issues = check_publication({"report.md": text}, cited, {})
    assert [(i.hard, i.kind) for i in issues if i.kind == "meta_talk"] == [
        (False, "meta_talk")
    ]
    rows = extract_claims({"report.md": "Codex caches. [1]\n\n[1]"}, cited, "r")
    assert [r["sentence"] for r in rows] == ["Codex caches."]


def test_a_bold_lead_in_ends_its_own_sentence() -> None:
    from researchkit.citations import _sentences

    text = (
        "**Codex drops results.** Issue 35415 says so [1]. Modes are hidden [2]. "
        "**Claude Code.** `WebSearch` returns titles [3]."
    )
    assert [s for _, s in _sentences(text)] == [
        "Codex drops results.**",
        "Issue 35415 says so [1].",
        "Modes are hidden [2].",
        "**Claude Code.**",
        "`WebSearch` returns titles [3].",
    ]
