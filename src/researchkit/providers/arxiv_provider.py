"""arXiv provider — the preprint slice, where new research lands first.

OpenAlex indexes *published* works: peer review adds months. arXiv carries
the preprint, often the same week the work is done, which for fast-moving
fields is the difference between current and historical. That gap is why
``mvanhorn/last30days-skill`` ships both, and why this sits alongside the
OpenAlex provider rather than replacing it.

Free and keyless via the arXiv Atom API, sorted by submission date and
windowed by ``--days``. Self-gates like OpenAlex: a topic with no matching
preprints reports that plainly instead of padding the report with whatever
the fuzzy search returned.
"""

from __future__ import annotations

import datetime as dt
import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import quote

import requests

from researchkit.network_retry import with_network_retry
from researchkit.providers.base import (
    BaseProvider,
    ProviderResult,
    Source,
    SourceType,
)

ARXIV_API_URL = "https://export.arxiv.org/api/query"
_ATOM = "{http://www.w3.org/2005/Atom}"

_MAX_RESULTS = 20
_MIN_RELEVANT = 2


def _text(node: ET.Element | None) -> str:
    return " ".join((node.text or "").split()) if node is not None else ""


class ArxivProvider(BaseProvider):
    """Recent arXiv preprints for a topic (keyless)."""

    provider_name = "arxiv"
    model_name = "arxiv-atom"

    def __init__(
        self,
        sources: set[str] | None = None,
        model: str | None = None,
    ) -> None:
        """Initialize the arXiv provider (keyless; no api_key)."""
        self.sources = sources or {"social", "web"}
        if model:
            self.model_name = model

    def _search(self, topic: str, days: int) -> list[dict[str, Any]]:
        """Newest-first preprints matching the topic, filtered to the window."""
        query = quote(f'all:"{topic[:200]}"')
        url = (
            f"{ARXIV_API_URL}?search_query={query}"
            f"&sortBy=submittedDate&sortOrder=descending"
            f"&max_results={_MAX_RESULTS}"
        )

        def _get() -> str:
            resp = requests.get(url, timeout=(10, 60))
            resp.raise_for_status()
            return resp.text

        feed = with_network_retry(
            _get, label="arxiv.query", provider=self.provider_name
        )
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=max(days, 1))
        papers: list[dict[str, Any]] = []
        try:
            root = ET.fromstring(feed)
        except ET.ParseError as e:
            raise ValueError(f"unparseable arXiv feed: {e}") from e
        for entry in root.findall(f"{_ATOM}entry"):
            published = _text(entry.find(f"{_ATOM}published"))
            try:
                when = dt.datetime.fromisoformat(published.replace("Z", "+00:00"))
            except ValueError:
                continue
            if when < cutoff:
                continue
            papers.append(
                {
                    "title": _text(entry.find(f"{_ATOM}title")),
                    "summary": _text(entry.find(f"{_ATOM}summary")),
                    "url": _text(entry.find(f"{_ATOM}id")),
                    "published": published[:10],
                    "authors": [
                        _text(a.find(f"{_ATOM}name"))
                        for a in entry.findall(f"{_ATOM}author")
                    ],
                }
            )
        return papers

    @staticmethod
    def _pdf_url(abs_url: str) -> str:
        """arXiv abs page -> its PDF, the citable artifact."""
        return re.sub(r"/abs/", "/pdf/", abs_url)

    def _to_sources(self, papers: list[dict[str, Any]]) -> list[Source]:
        return [
            Source(
                url=p["url"],
                title=p["title"],
                snippet=p["summary"][:300] or None,
                date=p["published"],
                source_type=SourceType.WEB,
            )
            for p in papers
            if p.get("url")
        ]

    @staticmethod
    def _format(papers: list[dict[str, Any]]) -> str:
        lines = []
        for p in papers:
            authors = ", ".join(p["authors"][:4]) + (
                " et al." if len(p["authors"]) > 4 else ""
            )
            lines.append(
                f"- **{p['title']}** ({p['published']})\n"
                f"  {authors}\n"
                f"  {p['url']}\n"
                f"  {p['summary'][:400]}"
            )
        return "\n\n".join(lines)

    def fetch_insights(self, topic: str, days: int) -> ProviderResult:
        """Fetch recent preprints; self-gate when the topic isn't a research one."""
        self._log_start()
        try:
            self._log_query("web")
            papers = self._search(topic, days)
            if len(papers) < _MIN_RELEVANT:
                self._log_done(0, 0)
                return ProviderResult(
                    provider=self.provider_name,
                    model=self.model_name,
                    raw_text=(
                        "# arXiv Preprints\n\n*No matching preprints in the "
                        f"last {days} days ({len(papers)} hits) — nothing "
                        "reported.*"
                    ),
                    sources=[],
                    meta={"papers": len(papers), "relevant": False},
                )
            sources = self._to_sources(papers)
            text = (
                f"# arXiv Preprints\n\n{len(papers)} preprints in the last "
                f"{days} days, newest first.\n\n{self._format(papers)}"
            )
            self._log_done(len(sources), len(text))
            return ProviderResult(
                provider=self.provider_name,
                model=self.model_name,
                raw_text=text,
                sources=sources,
                meta={"papers": len(papers), "relevant": True},
            )
        except Exception as e:
            return self._create_error_result(f"arXiv API error: {e}")

    def summarize_result(self, raw_text: str, topic: str) -> str:
        """Metadata API (no LLM of its own): return a truncated listing."""
        return raw_text[:2000] + "..." if len(raw_text) > 2000 else raw_text
