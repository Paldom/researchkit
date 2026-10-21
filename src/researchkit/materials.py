"""Download the sources a research run cited into the project folder.

After a run, ``report.md``/``result.json`` reference dozens of source URLs.
This module fetches those pages (SSRF-guarded, size-capped, politely paced)
and stores readable extracts under ``projects/<name>/materials/``:

    materials/
    ├── index.json            # manifest: every cited URL with fetch status
    ├── 001-reddit-com-....md # one frontmattered markdown file per source
    └── ...

The files use flat ``key: value`` frontmatter so downstream knowledge tools
(e.g. brainkit) can ingest them without a YAML parser. Failures are recorded,
never raised: a dead link must not sink the archive of 40 live ones.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html as html_lib
import json
import logging
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit

from researchkit.project import Project
from researchkit.safe_io import atomic_write_text, safe_fetch_text
from researchkit.urls import canonical_url, strip_tracking
from researchkit.utils import slugify

logger = logging.getLogger(__name__)

MATERIALS_DIRNAME = "materials"
MANIFEST_FILENAME = "index.json"

# 0 = fetch every cited source. The archive IS the knowledge base a run
# hands to brainkit/skillskit, so a truncated one silently drops evidence
# the report cites. Cap it per-run with --materials-limit when you must.
DEFAULT_LIMIT = 0
DEFAULT_TIMEOUT_S = 10.0
DEFAULT_MAX_BYTES = 500_000
DEFAULT_DELAY_S = 0.2
# Fetches run this wide; the delay above still paces each worker. A run cites
# a few hundred URLs across many hosts, so serial fetching was minutes of
# waiting on timeouts one at a time.
DEFAULT_WORKERS = 8
MIN_EXTRACT_CHARS = 200
# The frozen manifest grammar: origins are "http", "cached", "summary", or
# "connector:<kind>" with kind in this set. Downstream consumers (brainkit)
# rely on it; unknown kinds are clamped, never emitted.
_CONTENT_KINDS = ("article", "transcript", "summary")

# Decoded bodies that start with these markers are binary payloads that
# slipped through content negotiation; store the citation, skip the body.
_BINARY_MARKERS = ("%PDF", "\x89PNG", "PK\x03\x04", "GIF8")
_FETCH_HEADERS = {"User-Agent": "researchkit-materials/0.1 (+research archive)"}


@dataclass
class SourceRef:
    """One cited URL with everything the run knew about it."""

    url: str
    title: str = ""
    source_type: str = ""
    published: str = ""  # publication date, when the citation carried one
    providers: list[str] = field(default_factory=list)
    # Connector-provided canonical text (Medium article, YouTube transcript):
    # archived directly, no HTTP re-query. See EXTRAS.md.
    content: str = ""
    content_kind: str = ""
    # Rendered SiteItemSummary markdown — fallback body when a fetch comes
    # back empty/failed so the source still lands in the knowledge base.
    summary_md: str = ""


@dataclass
class MaterialEntry:
    """Manifest row for one cited URL."""

    url: str
    status: str  # fetched | failed | binary | empty | skipped_scheme | skipped_limit
    origin: str = ""  # http | connector:<kind> | summary (set on fetched rows)
    file: str | None = None
    final_url: str | None = None
    title: str = ""
    source_type: str = ""
    providers: list[str] = field(default_factory=list)
    chars: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            k: v for k, v in self.__dict__.items() if v not in (None, "", [], 0)
        } | {
            "url": self.url,
            "status": self.status,
        }


class _TextExtractor(HTMLParser):
    """Readable-text extraction: drops script/style/nav chrome, keeps headings."""

    _SKIP: ClassVar[set[str]] = {
        "script",
        "style",
        "noscript",
        "svg",
        "template",
        "iframe",
    }
    _HEADINGS: ClassVar[dict[str, str]] = {
        "h1": "# ",
        "h2": "## ",
        "h3": "### ",
        "h4": "#### ",
    }
    _BREAKS: ClassVar[set[str]] = {
        "p",
        "div",
        "li",
        "br",
        "tr",
        "section",
        "article",
        "blockquote",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self._HEADINGS:
            self._parts.append(f"\n\n{self._HEADINGS[tag]}")
        elif tag in self._BREAKS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self._HEADINGS or tag in self._BREAKS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
            return
        if data.strip():
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        out: list[str] = []
        blank = 0
        for line in lines:
            if line:
                out.append(line)
                blank = 0
            else:
                blank += 1
                if blank <= 1:
                    out.append("")
        return "\n".join(out).strip()


def extract_readable_text(html: str) -> tuple[str, str]:
    """Return ``(page_title, readable_text)`` for an HTML document.

    Non-HTML text (plain text, JSON, markdown served as text) passes through
    unchanged with an empty title.
    """
    lowered = html[:2048].lower()
    if "<html" not in lowered and "<body" not in lowered and "<p" not in lowered:
        return "", html.strip()
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # malformed markup: fall back to what was parsed
        logger.debug("HTML parse error; using partial extraction", exc_info=True)
    return " ".join(parser.title.split()), parser.text()


def _clean_citation_url(url: str) -> str:
    """Canonical form stored in frontmatter (see :func:`researchkit.urls.canonical_url`)."""
    return canonical_url(url)


def _normalize_url(url: str) -> str:
    """Dedup key: the same canonical identity (one rule, not two copies)."""
    return canonical_url(url)


def _fetch_url(url: str) -> str:
    """URL variant to actually fetch.

    www.reddit.com serves a JS shell with no extractable text; old.reddit.com
    returns the server-rendered thread. Tracking params are dropped.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = parts.netloc.lower()
    if host in ("www.reddit.com", "reddit.com"):
        host = "old.reddit.com"
    query = strip_tracking(parts.query)
    rebuilt = f"{parts.scheme}://{host}{parts.path}"
    return f"{rebuilt}?{query}" if query else rebuilt


def collect_source_refs(result: dict[str, Any]) -> list[SourceRef]:
    """All cited URLs from a run's ``result.json`` payload, deduplicated.

    Order: provider citations first (in provider order), then site-research
    items. A URL cited by several providers keeps every attribution.
    """
    refs: dict[str, SourceRef] = {}

    for provider_result in result.get("provider_results", []):
        provider = str(provider_result.get("provider", ""))
        for source in provider_result.get("sources", []) or []:
            url = str(source.get("url", "")).strip()
            if not url:
                continue
            key = _normalize_url(url)
            ref = refs.setdefault(
                key,
                SourceRef(
                    url=_clean_citation_url(url),
                    title=html_lib.unescape(str(source.get("title") or "")),
                    source_type=str(source.get("source_type") or ""),
                    published=str(source.get("date") or ""),
                ),
            )
            if provider and provider not in ref.providers:
                ref.providers.append(provider)
            if not ref.title and source.get("title"):
                ref.title = html_lib.unescape(str(source["title"]))

    site_research = result.get("site_research") or {}
    for site, items in (site_research.get("items_by_site") or {}).items():
        for item in items or []:
            url = str(item.get("url", "")).strip()
            if not url:
                continue
            key = _normalize_url(url)
            ref = refs.setdefault(
                key,
                SourceRef(
                    url=_clean_citation_url(url),
                    title=html_lib.unescape(str(item.get("title") or "")),
                ),
            )
            label = f"site:{site}"
            if label not in ref.providers:
                ref.providers.append(label)
            if not ref.content and item.get("content"):
                ref.content = str(item["content"])
                kind = str(item.get("content_kind") or "article")
                if kind not in _CONTENT_KINDS:
                    logger.warning(
                        "materials: unknown content_kind %r from %s clamped to 'article'",
                        kind,
                        url,
                    )
                    kind = "article"
                ref.content_kind = kind
            if not ref.summary_md and isinstance(item.get("summary"), dict):
                from researchkit.site_research.types import SiteItemSummary

                try:
                    ref.summary_md = SiteItemSummary.from_dict(
                        item["summary"]
                    ).to_markdown()
                except Exception:  # malformed legacy summaries never block
                    logger.debug("Unparseable site summary for %s", url)

    return list(refs.values())


def _frontmatter(pairs: dict[str, str]) -> str:
    """Flat ``key: value`` frontmatter; newlines/colons sanitized from values."""
    lines = ["---"]
    for key, value in pairs.items():
        clean = " ".join(str(value).split())
        lines.append(f"{key}: {clean}")
    lines.append("---")
    return "\n".join(lines)


# Interstitial/anti-bot page titles that must never become a source's title —
# they arrive both from fetched <title> tags and from provider citations
# (search-engine crawlers see the same walls).
_JUNK_TITLES = (
    "please wait",
    "just a moment",
    "access denied",
    "attention required",
    "verifying you are human",
    "are you a robot",
    "security check",
)


def _is_junk_title(title: str) -> bool:
    lowered = title.lower()
    return any(marker in lowered for marker in _JUNK_TITLES)


def _title_from_url(url: str) -> str:
    """Readable fallback title derived from the URL path."""
    parts = urlsplit(url)
    segments = [s for s in parts.path.split("/") if s]
    stem = segments[-1] if segments else parts.netloc
    words = stem.replace("-", " ").replace("_", " ").strip()
    host = parts.netloc.removeprefix("www.")
    return f"{words} ({host})" if words else host


def _pick_title(citation_title: str, page_title: str, url: str) -> str:
    """Best non-junk title: citation, then page, then URL-derived."""
    for candidate in (citation_title, page_title):
        cleaned = candidate.strip()
        if cleaned and not _is_junk_title(cleaned):
            return cleaned
    return _title_from_url(url)


def _file_matches_url(path: Path, url: str) -> bool:
    """True when an existing material file's frontmatter url matches ``url``.

    Guards idempotent reuse against filename collisions and stale files from
    earlier runs whose citation set has changed.
    """
    try:
        head = path.read_text(encoding="utf-8")[:2000]
    except OSError:
        return False
    return any(line.strip() == f"url: {url}" for line in head.splitlines()[:12])


def _cached_origin(path: Path) -> str:
    """Manifest origin for a reused material: ``summary`` stays ``summary``.

    A connector summary archived as a fallback is not source text; reusing it
    on a later run must not relabel it ``cached`` and let it pass as a page.
    """
    try:
        head = path.read_text(encoding="utf-8")[:2000]
    except OSError:
        return "cached"
    for line in head.splitlines()[:16]:
        if line.strip().replace('"', "") == "content_kind: summary":
            return "summary"
    return "cached"


def _material_filename(position: int, ref: SourceRef) -> str:
    host = urlsplit(ref.url).netloc.lower().removeprefix("www.")
    stem = slugify(f"{host} {ref.title}"[:80], max_length=60) or "source"
    return f"{position:03d}-{stem}.md"


def _write_material(
    path: Path,
    ref: SourceRef,
    result: dict[str, Any],
    *,
    body: str,
    content_kind: str = "",
    final_url: str = "",
    title: str = "",
) -> None:
    """Write one material file with the standard frontmatter.

    The single write path for ALL materials (fetched pages, connector
    canonical text, summary fallbacks) — shared so every file carries the
    same frontmatter contract, including ``content_digest``.
    """
    pairs = {
        "title": title or ref.title or ref.url,
        "url": ref.url,
        "final_url": final_url or ref.url,
        "source_type": ref.source_type or "web",
        "providers": ", ".join(ref.providers),
        "topic": str(result.get("topic", "")),
        "fetched_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    }
    if content_kind:
        pairs["content_kind"] = content_kind
    if ref.published:
        pairs["published"] = ref.published
    # Content lineage for downstream knowledge tools (backlog rk+bk):
    # detects changed source content across re-downloads and gives
    # cross-run dedup a second key beyond the URL.
    pairs["content_digest"] = hashlib.sha256(body.strip().encode()).hexdigest()[:16]
    atomic_write_text(path, f"{_frontmatter(pairs)}\n\n{body.strip()}\n")


def _write_summary_fallback(
    path: Path,
    ref: SourceRef,
    result: dict[str, Any],
    entry: MaterialEntry,
    filename: str,
) -> bool:
    """Archive the connector's summary when the page itself is unreachable.

    A cited summary note in the knowledge base beats a missing source
    (EXTRAS.md). Returns True when a fallback material was written.
    """
    if not ref.summary_md:
        return False
    _write_material(path, ref, result, body=ref.summary_md, content_kind="summary")
    entry.status = "fetched"
    entry.origin = "summary"
    entry.file = filename
    entry.title = ref.title or ref.url
    entry.chars = len(ref.summary_md)
    logger.info("materials: stored summary fallback for %s", ref.url)
    return True


def _repo_extract_entries(
    project: Project, result: dict[str, Any]
) -> list[dict[str, Any]]:
    """Manifest rows for repo extracts already written under ``repos/``.

    Recorded, never re-fetched: the extract was harvested during the run from a
    clone that no longer exists. The manifest may only name files inside
    ``materials/`` (brainkit fails closed on anything else, by design), so the
    extract is copied in under a ``repo-`` prefix and marked with its origin so
    consumers can tell it apart from a fetched web page.
    """
    rows: list[dict[str, Any]] = []
    materials_dir = project.path / MATERIALS_DIRNAME
    for extract in result.get("repo_extracts") or []:
        if not isinstance(extract, dict) or not extract.get("extract_file"):
            continue
        path = project.path / "repos" / str(extract["extract_file"])
        if not path.is_file():
            continue
        copy_name = f"repo-{Path(str(extract['extract_file'])).name}"
        materials_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, materials_dir / copy_name)
        rows.append(
            {
                "url": str(extract.get("url") or ""),
                "status": "fetched",
                "origin": "repo_extract",
                "file": copy_name,
                "title": str(extract.get("slug") or ""),
                "source_type": "repository",
                "chars": path.stat().st_size,
            }
        )
    return rows


def download_materials(
    project: Project,
    *,
    limit: int = DEFAULT_LIMIT,
    timeout: float = DEFAULT_TIMEOUT_S,
    max_bytes: int = DEFAULT_MAX_BYTES,
    delay: float = DEFAULT_DELAY_S,
    refresh: bool = False,
    workers: int = DEFAULT_WORKERS,
) -> dict[str, Any]:
    """Fetch a project's cited sources into ``materials/`` and write a manifest.

    Args:
        project: A researchkit project that has been run (``result.json`` exists).
        limit: Max sources to fetch (0 = no limit); the rest are recorded as
            ``skipped_limit`` so downstream consumers see the full citation set.
        timeout: Per-request timeout in seconds.
        max_bytes: Per-page size cap.
        delay: Pause before each fetch, per worker (politeness; also smooths
            rate limits).
        refresh: Re-fetch URLs whose material file already exists.
        workers: How many fetches run at once.

    Returns:
        The manifest dict (also written to ``materials/index.json``).
    """
    if not project.result_path.is_file():
        raise FileNotFoundError(
            f"{project.result_path} not found — run the project before "
            "downloading materials."
        )
    result = json.loads(project.result_path.read_text(encoding="utf-8"))
    refs = collect_source_refs(result)
    limit = max(0, limit)  # negative would otherwise skip everything

    materials_dir = project.path / MATERIALS_DIRNAME
    materials_dir.mkdir(exist_ok=True)

    entries: list[MaterialEntry] = []
    # (ref, entry, path, filename) still needing the network, fetched below.
    pending: list[tuple[SourceRef, MaterialEntry, Path, str]] = []
    for position, ref in enumerate(refs, start=1):
        entry = MaterialEntry(
            url=ref.url,
            status="pending",
            title=ref.title,
            source_type=ref.source_type,
            providers=list(ref.providers),
        )
        entries.append(entry)

        scheme = urlsplit(ref.url).scheme.lower()
        if scheme not in ("http", "https"):
            entry.status = "skipped_scheme"
            continue
        if limit and len(pending) >= limit:
            entry.status = "skipped_limit"
            continue

        filename = _material_filename(position, ref)
        path = materials_dir / filename
        if path.exists() and not refresh and _file_matches_url(path, ref.url):
            entry.status = "fetched"
            entry.origin = _cached_origin(path)
            entry.file = filename
            entry.chars = len(path.read_text(encoding="utf-8"))
            continue

        if ref.content:
            # Connector already holds the canonical text (article/transcript):
            # archive it directly — the platform is never re-queried.
            _write_material(
                path, ref, result, body=ref.content, content_kind=ref.content_kind
            )
            entry.status = "fetched"
            entry.origin = f"connector:{ref.content_kind or 'article'}"
            entry.file = filename
            entry.title = ref.title or ref.url
            entry.chars = len(ref.content)
            continue

        pending.append((ref, entry, path, filename))

    def _fetch_one(job: tuple[SourceRef, MaterialEntry, Path, str]) -> None:
        ref, entry, path, filename = job
        if delay:
            time.sleep(delay)
        body, final_url = safe_fetch_text(
            _fetch_url(ref.url),
            timeout=timeout,
            max_bytes=max_bytes,
            headers=_FETCH_HEADERS,
        )
        if body is None:
            if _write_summary_fallback(path, ref, result, entry, filename):
                return
            entry.status = "failed"
            logger.info("materials: fetch failed/blocked for %s", ref.url)
            return
        if body.lstrip().startswith(_BINARY_MARKERS):
            entry.status = "binary"
            logger.info("materials: binary payload skipped for %s", ref.url)
            return

        page_title, text = extract_readable_text(body)
        if len(text) < MIN_EXTRACT_CHARS:
            if _write_summary_fallback(path, ref, result, entry, filename):
                return
            # JS shells (x.com, app-only pages) return chrome with no content;
            # record the citation but don't pollute the archive with husks.
            entry.status = "empty"
            logger.info("materials: no extractable text for %s", ref.url)
            return
        title = _pick_title(ref.title, page_title, ref.url)
        _write_material(path, ref, result, body=text, final_url=final_url, title=title)
        entry.status = "fetched"
        entry.origin = "http"
        entry.file = filename
        entry.final_url = final_url
        entry.title = title
        entry.chars = len(text)

    # Each job owns its own entry and file, so the workers never share state;
    # the manifest below reads the entries only after every job has finished.
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for _ in pool.map(_fetch_one, pending):
            pass  # re-raises a worker's exception (none expected past safe_fetch_text)

    fetched = sum(1 for e in entries if e.status == "fetched")
    attempts = len(pending)  # network attempts — the limit bounds these, not successes

    # Repository extracts are materials too: they are archived source text a
    # downstream brain should ingest, and listing them here means one manifest
    # describes the whole pack instead of two half-descriptions of it. They are
    # already on disk under repos/, so this records rather than re-fetches.
    repo_entries = _repo_extract_entries(project, result)

    manifest = {
        "topic": result.get("topic", ""),
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "total_cited": len(refs),
        "fetched": fetched,
        "attempted": attempts,
        "repo_extracts": len(repo_entries),
        "entries": [e.to_dict() for e in entries] + repo_entries,
    }
    atomic_write_text(
        materials_dir / MANIFEST_FILENAME,
        json.dumps(manifest, indent=2, ensure_ascii=False),
    )
    logger.info(
        "materials: %d/%d sources archived for %s",
        fetched,
        len(refs),
        project.name,
    )
    return manifest
