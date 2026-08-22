"""Hacker News provider — the developer-consensus slice, scored by upvotes.

Every other provider hands back what a model or an index *thinks* is
relevant. HN hands back what a few hundred thousand engineers voted on: a
story's points and comment count are the signal, not a relevance score. That
is the idea worth borrowing from ``mvanhorn/last30days-skill`` — search the
people, not the editors.

Free and keyless via the Algolia HN API. Results are windowed by ``--days``
using ``created_at_i`` and sorted by points, so a thread's standing in the
community decides what makes the report.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from typing import Any

import requests

from researchkit.network_retry import with_network_retry
from researchkit.providers.base import (
    BaseProvider,
    ProviderResult,
    Source,
    SourceType,
)

HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search"
HN_ITEM_URL = "https://news.ycombinator.com/item?id="

_MAX_RESULTS = 30
# Below this a "story" is one person shouting into an empty room, not the
# developer consensus this provider exists to report.
_MIN_POINTS = 5
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str | None) -> str:
    """HN comment/story text is HTML; flatten it to plain text."""
    if not text:
        return ""
    return " ".join(html.unescape(_TAG_RE.sub(" ", text)).split())


class HackerNewsProvider(BaseProvider):
    """Upvote-scored Hacker News stories and comments (keyless)."""

    provider_name = "hackernews"
    model_name = "hn-algolia"

    def __init__(
        self,
        sources: set[str] | None = None,
        model: str | None = None,
    ) -> None:
        """Initialize the Hacker News provider (keyless; no api_key)."""
        self.sources = sources or {"social", "web"}
        if model:
            self.model_name = model

    def _search(self, topic: str, days: int) -> list[dict[str, Any]]:
        """Query the Algolia HN index for stories inside the window."""
        since = dt.datetime.now(dt.UTC) - dt.timedelta(days=max(days, 1))
        params: dict[str, Any] = {
            "query": topic[:300],
            "tags": "story",
            "numericFilters": f"created_at_i>{int(since.timestamp())}",
            "hitsPerPage": _MAX_RESULTS,
        }

        def _get() -> list[dict[str, Any]]:
            resp = requests.get(HN_SEARCH_URL, params=params, timeout=(10, 60))
            resp.raise_for_status()
            data = resp.json()
            hits = data.get("hits") if isinstance(data, dict) else None
            if not isinstance(hits, list):
                raise ValueError("unexpected Hacker News response shape")
            return [h for h in hits if isinstance(h, dict)]

        hits = with_network_retry(_get, label="hn.search", provider=self.provider_name)
        ranked = [h for h in hits if int(h.get("points") or 0) >= _MIN_POINTS]
        ranked.sort(key=lambda h: int(h.get("points") or 0), reverse=True)
        return ranked

    @staticmethod
    def _discussion_url(hit: dict[str, Any]) -> str:
        return f"{HN_ITEM_URL}{hit.get('objectID', '')}"

    def _to_sources(self, hits: list[dict[str, Any]]) -> list[Source]:
        """Cite the HN thread itself — that is where the votes and argument live."""
        sources: list[Source] = []
        seen: set[str] = set()
        for hit in hits:
            url = self._discussion_url(hit)
            if not hit.get("objectID") or url in seen:
                continue
            seen.add(url)
            sources.append(
                Source(
                    url=url,
                    title=hit.get("title") or hit.get("story_title"),
                    snippet=_strip_html(hit.get("story_text"))[:300] or None,
                    date=(hit.get("created_at") or "")[:10] or None,
                    source_type=SourceType.SOCIAL,
                )
            )
            linked = str(hit.get("url") or "")
            if linked and linked not in seen:
                # The submitted article is a distinct, citable source.
                seen.add(linked)
                sources.append(
                    Source(
                        url=linked,
                        title=hit.get("title"),
                        date=(hit.get("created_at") or "")[:10] or None,
                        source_type=SourceType.WEB,
                    )
                )
        return sources

    def _format(self, hits: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for hit in hits:
            points = int(hit.get("points") or 0)
            comments = int(hit.get("num_comments") or 0)
            title = hit.get("title") or "(untitled)"
            when = (hit.get("created_at") or "")[:10]
            body = _strip_html(hit.get("story_text"))[:400]
            lines.append(
                f"- **{title}** — {points} points, {comments} comments, {when}\n"
                f"  Discussion: {self._discussion_url(hit)}"
                + (f"\n  Link: {hit['url']}" if hit.get("url") else "")
                + (f"\n  {body}" if body else "")
            )
        return "\n".join(lines)

    def fetch_insights(self, topic: str, days: int) -> ProviderResult:
        """Fetch upvote-ranked HN discussion for the topic."""
        self._log_start()
        try:
            self._log_query("social")
            hits = self._search(topic, days)
            if not hits:
                self._log_done(0, 0)
                return ProviderResult(
                    provider=self.provider_name,
                    model=self.model_name,
                    raw_text=(
                        "# Hacker News\n\n*No stories above "
                        f"{_MIN_POINTS} points in the last {days} days.*"
                    ),
                    sources=[],
                    meta={"stories": 0},
                )
            sources = self._to_sources(hits)
            top = hits[0]
            text = (
                "# Hacker News\n\n"
                f"{len(hits)} stories in the last {days} days, ranked by points "
                f"(top: {int(top.get('points') or 0)}). Points measure "
                "attention, not correctness: treat them as a signal of what "
                "developers engaged with, never as evidence a claim is true."
                "\n\n"
                f"{self._format(hits)}"
            )
            self._log_done(len(sources), len(text))
            return ProviderResult(
                provider=self.provider_name,
                model=self.model_name,
                raw_text=text,
                sources=sources,
                meta={
                    "stories": len(hits),
                    "top_points": int(top.get("points") or 0),
                    "total_comments": sum(
                        int(h.get("num_comments") or 0) for h in hits
                    ),
                },
            )
        except Exception as e:
            return self._create_error_result(f"Hacker News API error: {e}")

    def summarize_result(self, raw_text: str, topic: str) -> str:
        """Metadata API (no LLM of its own): return a truncated listing."""
        return raw_text[:2000] + "..." if len(raw_text) > 2000 else raw_text
