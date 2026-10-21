"""The publication stage: one breadth-first report, a lead, an article.

PLAN 2.4-2.11. Replaces four independent writers that restated the same
answer under the same headings (14-21k-word report.md). Order:

1. Evidence: numbered sources and archived passages (:mod:`.evidence`).
2. Body: one writer, H2 finding sections from per-section passages.
3. Lead + article, in parallel, both from the finished body: the TL;DR and
   "In brief" (written last, STORM's lead-last), and the article as a second
   render of the same cited claims.
4. Citations renumbered in code, claims extracted, integrity gate
   (:mod:`.citations`).

All writers run on the preset's Claude writer model through the existing
Claude Code transport (tool lockdown, budget, quota stop). Quality is never
traded for speed (principle 8).
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from researchkit.citations import (
    CitedDocuments,
    Issue,
    check_publication,
    extract_claims,
    renumber,
)
from researchkit.evidence import (
    Evidence,
    EvidenceSource,
    load_evidence,
    plan_sections,
    select_passages,
)
from researchkit.final_summary import ArticleGenerator, ClaudeFinalSummaryGenerator
from researchkit.prompts import (
    get_article_from_report_user_prompt,
    get_article_system_prompt,
    get_report_body_system_prompt,
    get_report_body_user_prompt,
    get_report_lead_system_prompt,
    get_report_lead_user_prompt,
)
from researchkit.urls import canonical_url

if TYPE_CHECKING:
    from researchkit.aggregator import InsightBundle
    from researchkit.system_config import EffectiveModels

logger = logging.getLogger(__name__)

_LINK = re.compile(r"\]\((https?://[^\s)]+)\)")


class _Writer(ClaudeFinalSummaryGenerator):
    """A Claude Code writer with a fixed system prompt (shared transport)."""

    def __init__(
        self, system_prompt: str, *, model: str, max_budget: float, stage: str
    ) -> None:
        super().__init__(model=model, max_budget=max_budget)
        self._system = system_prompt
        self.stage_name = stage
        self.display_name = stage.replace("_", " ")

    def get_system_prompt(self) -> str:
        return self._system

    def write(self, user_prompt: str) -> str | None:
        text = self._run_claude(user_prompt)
        return _strip_fence(text) if text else None


def _strip_fence(text: str) -> str:
    """Drop a ```markdown fence a writer wrapped its answer in."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-z]*\n", "", t)
        t = re.sub(r"\n```\s*$", "", t)
    return t.strip()


@dataclass
class Publication:
    """Everything the publication stage produced."""

    report_markdown: str
    article_markdown: str | None
    evidence_markdown: str
    providers_markdown: str
    lead: str | None
    body: str | None
    sources: list[EvidenceSource]
    claims: list[dict[str, Any]] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def hard_issues(self) -> list[Issue]:
        return [i for i in self.issues if i.hard]

    def summary(self) -> dict[str, Any]:
        """The ``publication`` block of result.json."""
        return {
            "version": 2,
            "sources_cited": len(self.sources),
            "claims": len(self.claims),
            "hard_issues": [i.to_dict() for i in self.hard_issues],
            "advisories": sum(1 for i in self.issues if not i.hard),
            "errors": self.errors,
            "lead_written": self.lead is not None,
            "body_written": self.body is not None,
        }


def _writers(em: EffectiveModels | None) -> tuple[str, float]:
    """Model and budget the preset gives its Claude writers."""
    base = ArticleGenerator.from_effective_models(em)
    return base.model, base.max_budget


def _sources_md(sources: list[EvidenceSource]) -> str:
    lines = ["## Sources", ""]
    for s in sources:
        label = s.title.strip() or s.url
        note = "" if s.archived else " *(not archived)*"
        lines.append(f"{s.n}. [{label}]({s.url}){note}")
    return "\n".join(lines)


def _fallback_lead(body: str) -> str:
    """Deterministic lead when the lead writer fails: each section's first sentence."""
    bullets = []
    for m in re.finditer(r"^##\s+(.+?)\s*\n+(.+?)(?:\n\n|$)", body, re.M | re.S):
        heading = m.group(1).strip()
        if heading.lower().startswith("what the sources did not answer"):
            continue
        first = re.split(r"(?<=[.!?])\s+", m.group(2).strip())[0]
        bullets.append(f"- **{heading}**: {first}")
    tldr = bullets[0].split(": ", 1)[-1] if bullets else ""
    return f"## TL;DR\n\n{tldr}\n\n## In brief\n\n" + "\n".join(bullets)


def _article_link_issues(article: str, evidence: Evidence) -> list[Issue]:
    known = {s.url for s in evidence.sources}
    return [
        Issue(True, "article_off_list_url", url[:200])
        for url in sorted({canonical_url(u) for u in _LINK.findall(article)})
        if url not in known
    ]


def render_failure_article(topic: str, run_status: dict[str, Any]) -> str:
    """An honest article when research produced nothing citable (no LLM)."""
    slots = run_status.get("slots", {})
    lines = [
        f"# Research on “{topic}” did not produce citable findings",
        "",
        "This run could not establish findings with sources, so there is no "
        "article to publish. What happened:",
        "",
    ]
    lines += [f"- {reason}" for reason in run_status.get("reasons", [])]
    if slots:
        lines += ["", "Per research slot:", ""]
        lines += [f"- {name}: {state}" for name, state in sorted(slots.items())]
    lines += [
        "",
        "Re-run after the failing slots recover (`researchkit doctor --probe` "
        "shows quota and login state per harness).",
    ]
    return "\n".join(lines)


def _providers_md(bundle: InsightBundle) -> str:
    lines = ["# Per-provider summaries", ""]
    for r in bundle.provider_results:
        lines.append(f"## {r.provider} — {r.slot_state}")
        summary = bundle.individual_summaries.get(r.provider)
        if summary:
            lines += ["", summary.strip()]
        elif r.error:
            lines += ["", f"*{r.error[:300]}*"]
        lines.append("")
    return "\n".join(lines)


def _run_parallel(jobs: dict[str, Callable[[], str | None]]) -> dict[str, str | None]:
    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
        futures = {
            name: pool.submit(contextvars.copy_context().run, fn)
            for name, fn in jobs.items()
        }
        out: dict[str, str | None] = {}
        for name, fut in futures.items():
            try:
                out[name] = fut.result()
            except Exception as e:  # a writer failure degrades, never raises
                logger.warning(
                    f"{name} writer failed: {e}", extra={"stage": f"{name}_error"}
                )
                out[name] = None
        return out


def publish(
    *,
    topic: str,
    days: int,
    bundle: InsightBundle,
    project_dir: Path | None,
    effective_models: EffectiveModels | None,
    run_id: str,
    emit: Callable[[dict[str, Any]], None] = lambda evt: None,
) -> Publication:
    """Write and check the report and article from the run's evidence."""
    payload = bundle.to_dict()
    evidence = load_evidence(project_dir, payload)
    sections = plan_sections(bundle.meta_summary)
    picks = select_passages(sections, evidence)
    by_n = evidence.by_n()
    model, budget = _writers(effective_models)
    errors: dict[str, str] = {}

    packets = [
        (
            heading,
            orientation,
            [(p.n, by_n[p.n].title, p.text) for p in picks.get(heading, [])],
        )
        for heading, orientation in sections
    ]
    source_rows = [(s.n, s.title, s.url, s.archived) for s in evidence.sources]
    emit(
        {
            "stage": "report_body",
            "message": f"Writing report body ({len(sections)} sections)",
        }
    )
    body_writer = _Writer(
        get_report_body_system_prompt(),
        model=model,
        max_budget=budget,
        stage="report_body",
    )
    body = body_writer.write(
        get_report_body_user_prompt(
            topic, days, bundle.meta_summary, packets, source_rows
        )
    )
    if body is None:
        errors["report_body"] = body_writer.last_error or "no output"
        # The analysis is cited and still the best available body; say so.
        body = (
            "> The report writer failed; this is the cross-source analysis "
            f"the report would have been written from.\n\n{bundle.meta_summary.strip()}"
        )
        body_written = False
    else:
        body_written = True
    emit(
        {
            "stage": "report_body_done",
            "message": "Report body written" if body_written else "Report body skipped",
        }
    )

    lead_writer = _Writer(
        get_report_lead_system_prompt(),
        model=model,
        max_budget=budget,
        stage="report_lead",
    )
    article_writer = _Writer(
        get_article_system_prompt(), model=model, max_budget=budget, stage="article"
    )
    article_sources = [(s.n, s.title, s.url) for s in evidence.sources]
    emit({"stage": "report_lead", "message": "Writing lead and article"})
    outputs = _run_parallel(
        {
            "lead": lambda: lead_writer.write(get_report_lead_user_prompt(topic, body)),
            "article": lambda: article_writer.write(
                get_article_from_report_user_prompt(topic, days, body, article_sources)
            ),
        }
    )
    lead = outputs.get("lead")
    if lead is None:
        errors["report_lead"] = lead_writer.last_error or "no output"
    article = outputs.get("article")
    if article is None:
        errors["article"] = article_writer.last_error or "no output"
    emit({"stage": "report_lead_done", "message": "Lead and article written"})

    cited: CitedDocuments = renumber([lead or _fallback_lead(body), body], evidence)
    final_lead, final_body = cited.texts
    report_text = f"{final_lead}\n\n{final_body}"
    report_md = "\n\n".join(
        [
            f"# {topic}",
            report_text,
            _sources_md(cited.sources),
            "---\n\nDetail: [cross-source analysis](evidence.md) · "
            "[per-provider summaries](providers.md) · raw provider output in `result.json`.",
        ]
    )
    archive_text: dict[str, str] = {}
    if project_dir is not None:
        for s in cited.sources:
            if s.archived and s.file:
                with contextlib.suppress(OSError):
                    path = project_dir / "materials" / s.file
                    archive_text[s.url] = path.read_text(encoding="utf-8")
    issues = check_publication({"report.md": report_text}, cited, archive_text)
    if article:
        issues += _article_link_issues(article, evidence)
    claims = extract_claims({"report.md": report_text}, cited, run_id)
    evidence_md = (
        "# Cross-source analysis\n\nThe intermediate synthesis the report was written from.\n\n"
        + (bundle.meta_summary or "")
    )
    return Publication(
        report_markdown=report_md,
        article_markdown=article,
        evidence_markdown=evidence_md,
        providers_markdown=_providers_md(bundle),
        lead=lead,
        body=final_body if body_written else None,
        sources=cited.sources,
        claims=claims,
        issues=issues,
        errors=errors,
    )
