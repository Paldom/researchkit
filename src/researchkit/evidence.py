"""The evidence a report is written from: archived passages, numbered sources.

Writers used to receive the meta-summary, every provider summary and every
raw provider report at once: the same facts three times, and never the
archived pages themselves (digest 11: raw snippets beat synthesized notes;
section-scoped evidence raised citation accuracy 86.7% -> 93.4%). This module
turns a run's ``materials/`` into numbered sources plus paragraph-sized
passages, plans the report's sections, and selects each section's passages
within a word budget. Everything is deterministic: no model call.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from researchkit.urls import canonical_url

# Sources whose archived body is a synthesized summary or procedure listing
# never count as evidence (they are context, not a source's own text).
_NON_EVIDENCE_ORIGINS = ("summary",)
_PASSAGE_MIN_WORDS = 60
_PASSAGE_MAX_WORDS = 220
_MAX_PASSAGES_PER_SOURCE = 40
PER_SECTION_WORDS = 1500
TOTAL_WORDS = 9000
_MAX_PER_SOURCE_PER_SECTION = 3
_SKIP_HEADINGS = re.compile(
    r"^(tl;?dr|summary|overview|introduction|conclusion|sources?|references|"
    r"key references|what (the sources|we) did ?n[o']t (answer|know)|gaps?)\b",
    re.I,
)
_WORD = re.compile(r"[a-z0-9][a-z0-9+#.-]*", re.I)


@dataclass
class EvidenceSource:
    """One cited URL with its citation number and archive status."""

    n: int
    url: str
    title: str = ""
    status: str = ""  # manifest status: fetched, tos-blocked, failed, …
    file: str | None = None
    content_hash: str = ""
    providers: list[str] = field(default_factory=list)

    @property
    def archived(self) -> bool:
        return self.status == "fetched" and bool(self.file)


@dataclass
class Passage:
    """A paragraph-sized span of one archived source."""

    id: str  # "s<n>.p<k>"
    n: int
    text: str

    @property
    def words(self) -> int:
        return len(self.text.split())


@dataclass
class Evidence:
    """Every cited source (numbered) and the passages of the archived ones."""

    sources: list[EvidenceSource] = field(default_factory=list)
    passages: list[Passage] = field(default_factory=list)

    def by_url(self) -> dict[str, EvidenceSource]:
        return {s.url: s for s in self.sources}

    def by_n(self) -> dict[int, EvidenceSource]:
        return {s.n: s for s in self.sources}


def _body_and_hash(path: Path) -> tuple[str, str]:
    """Material body without frontmatter, plus its ``content_digest``."""
    text = path.read_text(encoding="utf-8")
    digest = ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                if line.startswith("content_digest:"):
                    digest = line.split(":", 1)[1].strip()
            text = text[end + 4 :]
    return text.strip(), digest


def _split_passages(n: int, body: str) -> list[Passage]:
    """Paragraphs merged up to ~220 words; tiny fragments join the next one."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    chunks: list[str] = []
    current: list[str] = []
    words = 0
    for para in paragraphs:
        w = len(para.split())
        if current and words + w > _PASSAGE_MAX_WORDS and words >= _PASSAGE_MIN_WORDS:
            chunks.append("\n\n".join(current))
            current, words = [], 0
        if w > _PASSAGE_MAX_WORDS:  # one huge paragraph: split by sentences
            sentences = re.split(r"(?<=[.!?])\s+", para)
            buf: list[str] = []
            bw = 0
            for sent in sentences:
                sw = len(sent.split())
                if buf and bw + sw > _PASSAGE_MAX_WORDS:
                    chunks.append(" ".join(buf))
                    buf, bw = [], 0
                buf.append(sent)
                bw += sw
            if buf:
                current.append(" ".join(buf))
                words += bw
            continue
        current.append(para)
        words += w
    if current:
        chunks.append("\n\n".join(current))
    return [
        Passage(id=f"s{n}.p{k}", n=n, text=chunk)
        for k, chunk in enumerate(chunks[:_MAX_PASSAGES_PER_SOURCE], start=1)
    ]


def load_evidence(project_dir: Path | None, payload: dict[str, Any]) -> Evidence:
    """Number every cited source and load passages from the archive.

    Numbering follows the manifest (provider citation order). Without a
    manifest (a library run with no project folder) the cited URLs from
    ``payload`` are numbered and no passages exist; writers then work from
    the analysis alone, as before.
    """
    evidence = Evidence()
    manifest: dict[str, Any] | None = None
    if project_dir is not None:
        index = project_dir / "materials" / "index.json"
        if index.is_file():
            try:
                manifest = json.loads(index.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                manifest = None
    entries: list[dict[str, Any]] = list((manifest or {}).get("entries") or [])
    if not entries:
        from researchkit.materials import collect_source_refs

        entries = [
            {
                "url": ref.url,
                "title": ref.title,
                "status": "",
                "providers": list(ref.providers),
            }
            for ref in collect_source_refs(payload)
        ]
    seen: set[str] = set()
    for entry in entries:
        url = canonical_url(str(entry.get("url") or ""))
        if not url or url in seen:
            continue
        seen.add(url)
        source = EvidenceSource(
            n=len(evidence.sources) + 1,
            url=url,
            title=str(entry.get("title") or ""),
            status=str(entry.get("status") or ""),
            file=entry.get("file"),
            providers=[str(p) for p in entry.get("providers") or []],
        )
        evidence.sources.append(source)
        origin = str(entry.get("origin") or "")
        if (
            not source.archived
            or origin in _NON_EVIDENCE_ORIGINS
            or project_dir is None
        ):
            continue
        path = project_dir / "materials" / str(source.file)
        if not path.is_file():
            continue
        body, digest = _body_and_hash(path)
        source.content_hash = digest
        evidence.passages.extend(_split_passages(source.n, body))
    return evidence


def plan_sections(meta_summary: str, *, max_sections: int = 5) -> list[tuple[str, str]]:
    """(heading, orientation text) per section, from the analysis's H2s.

    The meta-summary is the run's cross-provider analysis; its headings are
    the questions the evidence answers. They are retrieval hints for passage
    selection, never evidence. Generic headings (TL;DR, Sources…) are
    skipped. With fewer than two usable headings the report gets one
    "Findings" section over the whole analysis.
    """
    sections: list[tuple[str, str]] = []
    parts = re.split(r"^##\s+(.+?)\s*$", meta_summary or "", flags=re.M)
    for i in range(1, len(parts) - 1, 2):
        heading = parts[i].strip().strip("#").strip()
        if not heading or _SKIP_HEADINGS.match(heading):
            continue
        sections.append((heading, parts[i + 1].strip()))
    if len(sections) < 2:
        return [("Findings", (meta_summary or "").strip())]
    return sections[:max_sections]


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _WORD.findall(text) if len(t) > 2]


def select_passages(
    sections: list[tuple[str, str]],
    evidence: Evidence,
    *,
    per_section_words: int = PER_SECTION_WORDS,
    total_words: int = TOTAL_WORDS,
) -> dict[str, list[Passage]]:
    """Each section's best passages by idf-weighted overlap, within budget.

    At most three passages per source per section keeps one long page from
    crowding out independent sources; a passage goes to one section only.
    """
    if not evidence.passages or not sections:
        return {heading: [] for heading, _ in sections}
    df: Counter[str] = Counter()
    passage_tokens: dict[str, set[str]] = {}
    for passage in evidence.passages:
        toks = set(_tokens(passage.text))
        passage_tokens[passage.id] = toks
        df.update(toks)
    total = len(evidence.passages)

    def idf(token: str) -> float:
        return math.log((total + 1) / (df.get(token, 0) + 1)) + 1.0

    used: set[str] = set()
    remaining = total_words
    out: dict[str, list[Passage]] = {}
    per_section = min(per_section_words, max(total_words // max(len(sections), 1), 300))
    for heading, orientation in sections:
        query = Counter(_tokens(f"{heading} {heading} {orientation}"))
        scored = sorted(
            (
                (
                    sum(
                        idf(t) * min(c, 3)
                        for t, c in query.items()
                        if t in passage_tokens[p.id]
                    )
                    / math.sqrt(max(p.words, 1)),
                    p,
                )
                for p in evidence.passages
                if p.id not in used
            ),
            key=lambda pair: (-pair[0], pair[1].n, pair[1].id),
        )
        picked: list[Passage] = []
        words = 0
        per_source: Counter[int] = Counter()
        for score, passage in scored:
            if score <= 0 or words >= per_section or remaining <= 0:
                break
            if per_source[passage.n] >= _MAX_PER_SOURCE_PER_SECTION:
                continue
            picked.append(passage)
            used.add(passage.id)
            per_source[passage.n] += 1
            words += passage.words
            remaining -= passage.words
        out[heading] = picked
    return out
