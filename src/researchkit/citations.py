"""Citations in code: anchors, numbering, claims, and the integrity gate.

The writers cite with ``[n]`` from a numbered source list. Code owns the rest
(PLAN 2.5, 2.9, 2.11): inline URLs a writer used anyway are mapped to their
number, numbers are reassigned by first appearance across the documents
(STORM, ``storm_dataclass.py:188-206, 374-412``), unused sources drop out,
and every cited sentence becomes a claims row. The gate hard-fails only
what code can establish: unknown citations, chatbot artifacts,
"corroborated" on one origin. Number/quote presence and unarchived
citations are advisory, since presence is not support.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from researchkit.evidence import Evidence, EvidenceSource
from researchkit.urls import canonical_url, host_of, origin_key

_ANCHOR_GROUP = re.compile(r"\[(\d{1,3}(?:\s*[,;]\s*\d{1,3})*)\]")
_MD_LINK = re.compile(r"\[([^\]\n]{1,200})\]\((https?://[^\s)]+)\)")
_BARE_URL = re.compile(r"(?<![(\[<])\bhttps?://[^\s)\]>\"']+")
_ARTIFACTS = re.compile(
    r"\b(certainly!? here(?:'s| is)|as an ai\b|i hope this helps|"
    r"here(?:'s| is) (?:the|your) (?:report|article|overview)|let me know if)",
    re.I,
)
# Assertions only ("is corroborated", "corroborated by", "corroborates"):
# a sentence *about* corroboration rules is not a corroboration claim.
_CORROBORATED = re.compile(
    r"\b(?:(?:is|are|was|were|been|being|independently|widely|fully)\s+corroborated"
    r"|corroborated\s+by|corroborates?)\b",
    re.I,
)
_HEDGE = re.compile(
    r"\b(may|might|could|likely|unlikely|appears?|suggests?|reportedly|"
    r"according to|unverified|uncited|preliminary|claims?|estimated?|about|roughly)\b",
    re.I,
)
_NUMBER = re.compile(r"\b\d[\d,.]*%?")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[*])")


@dataclass
class Issue:
    """One integrity finding; ``hard`` ones fail the publication gate."""

    hard: bool
    kind: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"hard": self.hard, "kind": self.kind, "detail": self.detail}


@dataclass
class CitedDocuments:
    """Documents after renumbering, with the sources they actually cite."""

    texts: list[str]
    sources: list[EvidenceSource]  # in final [1..k] order
    issues: list[Issue] = field(default_factory=list)


def _urls_to_anchors(text: str, url_to_n: dict[str, int], issues: list[Issue]) -> str:
    """Inline links/URLs a writer used become ``[n]``; unknown ones are flagged."""

    def link(m: re.Match[str]) -> str:
        n = url_to_n.get(canonical_url(m.group(2)))
        if n is None:
            issues.append(Issue(True, "off_list_url", m.group(2)[:200]))
            return m.group(1)
        return f"{m.group(1)} [{n}]"

    def bare(m: re.Match[str]) -> str:
        url = m.group(0).rstrip(".,;:")
        n = url_to_n.get(canonical_url(url))
        if n is None:
            issues.append(Issue(True, "off_list_url", url[:200]))
            return ""
        return f"[{n}]"

    return _BARE_URL.sub(bare, _MD_LINK.sub(link, text))


def renumber(texts: list[str], evidence: Evidence) -> CitedDocuments:
    """Map anchors to sources and renumber them by first appearance."""
    issues: list[Issue] = []
    by_n = evidence.by_n()
    # Cosmetic variants of one page (www., trailing slash) share a number in
    # the report; their canonical identities stay distinct elsewhere.
    first_by_origin: dict[str, int] = {}
    alias: dict[int, int] = {}
    for src in evidence.sources:
        alias[src.n] = first_by_origin.setdefault(origin_key(src.url), src.n)
    url_to_n = {s.url: alias[s.n] for s in evidence.sources}
    texts = [_urls_to_anchors(t, url_to_n, issues) for t in texts]
    order: dict[int, int] = {}  # original n -> new number

    def collect(m: re.Match[str]) -> str:
        for raw in re.split(r"\s*[,;]\s*", m.group(1)):
            n = alias.get(int(raw), int(raw))
            if n in by_n and n not in order:
                order[n] = len(order) + 1
        return m.group(0)

    for t in texts:
        _ANCHOR_GROUP.sub(collect, t)

    def rewrite(m: re.Match[str]) -> str:
        nums: list[int] = []
        for raw in re.split(r"\s*[,;]\s*", m.group(1)):
            n = alias.get(int(raw), int(raw))
            if n not in by_n:
                issues.append(Issue(True, "unknown_citation", f"[{n}]"))
                continue
            if order[n] not in nums:
                nums.append(order[n])
        return "".join(f"[{k}]" for k in sorted(nums))

    renumbered = [_ANCHOR_GROUP.sub(rewrite, t) for t in texts]
    used = [by_n[orig] for orig, _ in sorted(order.items(), key=lambda kv: kv[1])]
    final = [
        EvidenceSource(
            n=i,
            url=s.url,
            title=s.title,
            status=s.status,
            file=s.file,
            content_hash=s.content_hash,
            providers=s.providers,
            origin=s.origin,
        )
        for i, s in enumerate(used, start=1)
    ]
    return CitedDocuments(texts=renumbered, sources=final, issues=issues)


def _sentences(text: str) -> list[tuple[str, str]]:
    """(section heading, sentence) pairs for prose and list lines."""
    heading = ""
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            continue
        if not stripped or stripped.startswith(("|---", "```")):
            continue
        for sent in _SENTENCE.split(stripped.lstrip("-*> ").strip()):
            if sent.strip():
                out.append((heading, sent.strip()))
    return out


def extract_claims(
    documents: dict[str, str], cited: CitedDocuments, run_id: str
) -> list[dict[str, Any]]:
    """One claims row per cited sentence and source (PLAN 2.9).

    The sentence is kept verbatim, with its hedge words and numbers, so
    brainkit and skillskit can carry qualifications forward instead of
    re-summarizing. IDs hash the sentence and the source identity, so they
    are stable across renumbering.
    """
    by_n = {s.n: s for s in cited.sources}
    rows: list[dict[str, Any]] = []
    for doc_name, text in documents.items():
        for heading, sentence in _sentences(text):
            nums = sorted(
                {
                    int(n)
                    for g in _ANCHOR_GROUP.findall(sentence)
                    for n in re.split(r"\s*[,;]\s*", g)
                }
            )
            if not nums:
                continue
            plain = re.sub(
                r"\s+([.,;:!?])", r"\1", _ANCHOR_GROUP.sub("", sentence)
            ).strip()
            for n in nums:
                source = by_n.get(n)
                if source is None:
                    continue
                key = f"{plain}\x00{source.url}\x00{source.content_hash}"
                rows.append(
                    {
                        "schema_version": 1,
                        "id": "sha256:" + hashlib.sha256(key.encode()).hexdigest()[:24],
                        "run_id": run_id,
                        "document": doc_name,
                        "section": heading,
                        "sentence": plain,
                        "canonical_url": source.url,
                        "content_hash": source.content_hash,
                        "archive_status": source.status or "unknown",
                        "material_file": source.file,
                        "hedges": sorted({h.lower() for h in _HEDGE.findall(plain)}),
                        "numbers": _NUMBER.findall(plain),
                        "co_cited": [by_n[m].url for m in nums if m != n and m in by_n],
                    }
                )
    return rows


def check_publication(
    documents: dict[str, str], cited: CitedDocuments, archive_text: dict[str, str]
) -> list[Issue]:
    """The integrity gate (PLAN 2.11): hard failures plus advisories.

    Hard: off-list or unknown citations (from renumbering), chatbot
    artifacts, a sentence calling a claim "corroborated" while citing one
    origin. Advisory: citations of sources with no archived text, and
    numbers in a cited sentence that appear in none of its archived sources.
    """
    issues = list(cited.issues)
    by_n = {s.n: s for s in cited.sources}
    for name, text in documents.items():
        for m in _ARTIFACTS.finditer(text):
            issues.append(Issue(True, "chatbot_artifact", f"{name}: {m.group(0)!r}"))
        for _, sentence in _sentences(text):
            nums = {
                int(n)
                for g in _ANCHOR_GROUP.findall(sentence)
                for n in re.split(r"\s*[,;]\s*", g)
            }
            cited_sources = [by_n[n] for n in nums if n in by_n]
            if (
                _CORROBORATED.search(sentence)
                and "uncorroborated" not in sentence.lower()
            ):
                # Hosts, folded: a syndicated copy shares its original's origin.
                origins = {s.origin or host_of(s.url) for s in cited_sources}
                if len(origins) < 2:
                    issues.append(
                        Issue(
                            True,
                            "single_origin_corroboration",
                            f"{name}: {sentence[:160]}",
                        )
                    )
            archived = [s for s in cited_sources if s.archived]
            for s in cited_sources:
                if not s.archived:
                    issues.append(
                        Issue(False, "unarchived_citation", f"[{s.n}] {s.url}")
                    )
            if archived:
                bodies = " ".join(archive_text.get(s.url, "") for s in archived)
                for number in _NUMBER.findall(_ANCHOR_GROUP.sub("", sentence)):
                    core = number.rstrip("%.,")
                    if len(core) >= 2 and core not in bodies:
                        issues.append(
                            Issue(
                                False,
                                "number_not_in_archive",
                                f"{number}: {sentence[:120]}",
                            )
                        )
    # One advisory per unarchived source is enough.
    seen: set[tuple[bool, str, str]] = set()
    unique: list[Issue] = []
    for issue in issues:
        key = (issue.hard, issue.kind, issue.detail)
        if key not in seen:
            seen.add(key)
            unique.append(issue)
    return unique
