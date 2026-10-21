"""Stack Exchange provider — the practitioner Q&A slice, scored by votes.

Hacker News tells you what developers are arguing about. Stack Exchange
tells you what they got stuck on and which answer actually worked — scored
by accepted answers and vote counts rather than by a relevance model. The
two are complements, which is why both exist here.

Free and keyless (the anonymous tier allows 300 requests/day, ample for one
query per run). Searches Stack Overflow by default; ``site`` can point at
any Stack Exchange site. Windowed by ``--days`` via ``fromdate``.
"""

from __future__ import annotations

import datetime as dt
import html
from typing import Any

import requests

from researchkit.network_retry import with_network_retry
from researchkit.providers.base import (
    BaseProvider,
    ProviderResult,
    Source,
    SourceType,
)
from researchkit.queries import keyword_query

SE_SEARCH_URL = "https://api.stackexchange.com/2.3/search/advanced"

_MAX_RESULTS = 25
_MIN_RELEVANT = 2


class StackExchangeProvider(BaseProvider):
    """Vote-scored Stack Exchange questions (keyless)."""

    provider_name = "stackexchange"
    model_name = "stackexchange"

    def __init__(
        self,
        sources: set[str] | None = None,
        model: str | None = None,
        site: str = "stackoverflow",
    ) -> None:
        """Initialize the Stack Exchange provider (keyless; no api_key)."""
        self.sources = sources or {"social", "web"}
        self.site = site
        if model:
            self.model_name = model

    def _search(self, topic: str, days: int) -> list[dict[str, Any]]:
        """Highest-voted questions matching the topic inside the window."""
        since = dt.datetime.now(dt.UTC) - dt.timedelta(days=max(days, 1))
        params: dict[str, Any] = {
            "order": "desc",
            "sort": "votes",
            "q": keyword_query(topic),
            "site": self.site,
            "pagesize": _MAX_RESULTS,
            "fromdate": int(since.timestamp()),
            "filter": "withbody",
        }

        def _get() -> list[dict[str, Any]]:
            # compressed: the API always gzips; requests handles it, but the
            # header keeps the anonymous quota accounting happy.
            resp = requests.get(
                SE_SEARCH_URL,
                params=params,
                timeout=(10, 60),
                headers={"Accept-Encoding": "gzip"},
            )
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise ValueError("unexpected Stack Exchange response shape")
            return [i for i in items if isinstance(i, dict)]

        return with_network_retry(
            _get, label="stackexchange.search", provider=self.provider_name
        )

    def _to_sources(self, items: list[dict[str, Any]]) -> list[Source]:
        sources: list[Source] = []
        for item in items:
            url = str(item.get("link") or "")
            if not url:
                continue
            when = item.get("creation_date")
            sources.append(
                Source(
                    url=url,
                    title=html.unescape(str(item.get("title") or "")),
                    snippet=(
                        f"{item.get('score', 0)} votes, "
                        f"{item.get('answer_count', 0)} answers"
                    ),
                    date=(
                        dt.datetime.fromtimestamp(when, dt.UTC).date().isoformat()
                        if isinstance(when, int)
                        else None
                    ),
                    source_type=SourceType.SOCIAL,
                )
            )
        return sources

    @staticmethod
    def _format(items: list[dict[str, Any]]) -> str:
        lines = []
        for item in items:
            title = html.unescape(str(item.get("title") or ""))
            accepted = "accepted answer" if item.get("is_answered") else "unanswered"
            tags = ", ".join(item.get("tags") or [])
            lines.append(
                f"- **{title}** — {item.get('score', 0)} votes, "
                f"{item.get('answer_count', 0)} answers, {accepted}\n"
                f"  {item.get('link', '')}" + (f"\n  Tags: {tags}" if tags else "")
            )
        return "\n".join(lines)

    def fetch_insights(self, topic: str, days: int) -> ProviderResult:
        """Fetch vote-ranked questions; self-gate when the topic isn't technical."""
        self._log_start()
        try:
            self._log_query("social")
            items = self._search(topic, days)
            if len(items) < _MIN_RELEVANT:
                self._log_done(0, 0)
                return ProviderResult(
                    provider=self.provider_name,
                    model=self.model_name,
                    raw_text=(
                        f"# Stack Exchange ({self.site})\n\n*No matching "
                        f"questions in the last {days} days ({len(items)} "
                        "hits) — nothing reported.*"
                    ),
                    sources=[],
                    meta={"questions": len(items), "relevant": False},
                )
            sources = self._to_sources(items)
            answered = sum(1 for i in items if i.get("is_answered"))
            text = (
                f"# Stack Exchange ({self.site})\n\n{len(items)} questions in "
                f"the last {days} days, ranked by votes; {answered} have an "
                f"accepted answer.\n\n{self._format(items)}"
            )
            self._log_done(len(sources), len(text))
            return ProviderResult(
                provider=self.provider_name,
                model=self.model_name,
                raw_text=text,
                sources=sources,
                meta={
                    "questions": len(items),
                    "relevant": True,
                    "answered": answered,
                    "top_score": max(int(i.get("score") or 0) for i in items),
                },
            )
        except Exception as e:
            return self._create_error_result(f"Stack Exchange API error: {e}")

    def summarize_result(self, raw_text: str, topic: str) -> str:
        """Metadata API (no LLM of its own): return a truncated listing."""
        return raw_text[:2000] + "..." if len(raw_text) > 2000 else raw_text
