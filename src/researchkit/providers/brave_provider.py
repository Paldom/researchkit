"""Brave Search provider — the independent-index slice.

Brave runs the only large independent English web index (not Google- or
Bing-derived), so its results can surface pages both big indexes miss and
its *discussions* vertical covers forum/Reddit threads directly. Plain REST
like Tavily: structured results in, no LLM synthesis.

Design constraints from the live evaluation: starter plans rate-limit hard
(~1 request/second), so the social and web queries run sequentially with a
pace delay; ``page_age`` gives real published dates (they flow to materials
frontmatter and brainkit's freshness ranking).
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests

from researchkit.network_retry import with_network_retry
from researchkit.providers.base import (
    BaseProvider,
    ProviderResult,
    Source,
    SourceType,
)

BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"

# Starter tiers allow ~1 req/s; pace the second query instead of tripping 429s.
_PACE_SECONDS = 1.1
_MAX_RESULTS = 20


class BraveProvider(BaseProvider):
    """Brave Search API provider (web + discussions verticals)."""

    provider_name = "brave"
    model_name = "brave-search"

    def __init__(
        self,
        api_key: str | None = None,
        sources: set[str] | None = None,
        model: str | None = None,
    ) -> None:
        """
        Initialize the Brave provider.

        Args:
            api_key: Brave API key (defaults to BRAVE_API_KEY env var)
            sources: Set of sources to query ("social", "web", or both)
            model: Display model name (overrides default)
        """
        self.api_key = api_key or os.getenv("BRAVE_API_KEY")
        self.sources = sources or {"social", "web"}
        if model:
            self.model_name = model

    @staticmethod
    def _days_to_freshness(days: int) -> str:
        """Convert days to Brave's freshness filter."""
        if days <= 1:
            return "pd"
        elif days <= 7:
            return "pw"
        elif days <= 31:
            return "pm"
        return "py"

    def _search(self, query: str, days: int, result_filter: str) -> dict[str, Any]:
        """Execute one Brave search (result_filter: "web" or "discussions,web")."""
        params = {
            "q": query[:400],
            "count": _MAX_RESULTS,
            "freshness": self._days_to_freshness(days),
            "result_filter": result_filter,
            "text_decorations": "false",
        }
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self.api_key or "",
        }

        def _get() -> dict[str, Any]:
            resp = requests.get(
                BRAVE_API_URL, params=params, headers=headers, timeout=(10, 60)
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise ValueError("unexpected Brave response shape")
            return data

        return with_network_retry(_get, label="brave.search", provider="brave")

    @staticmethod
    def _extract_results(data: dict[str, Any]) -> list[dict[str, Any]]:
        """Collect result objects from the web and discussions verticals."""
        results: list[dict[str, Any]] = []
        for key in ("discussions", "web"):
            block = data.get(key) or {}
            if isinstance(block, dict):
                items = block.get("results") or []
                if isinstance(items, list):
                    results.extend(r for r in items if isinstance(r, dict))
        return results

    @staticmethod
    def _results_to_sources(
        results: list[dict[str, Any]],
        source_type: SourceType,
        seen_urls: set[str],
    ) -> list[Source]:
        """Convert Brave results to Source objects (page_age -> published)."""
        sources: list[Source] = []
        for r in results:
            url = (r.get("url") or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            sources.append(
                Source(
                    url=url,
                    title=r.get("title"),
                    snippet=r.get("description"),
                    date=(r.get("page_age") or "")[:10] or None,
                    source_type=source_type,
                )
            )
        return sources

    @staticmethod
    def _format_results(results: list[dict[str, Any]]) -> str:
        """Render results as a citeable markdown list (Brave has no answer)."""
        lines: list[str] = []
        for r in results:
            title = r.get("title") or "Untitled"
            url = r.get("url") or ""
            desc = r.get("description") or ""
            age = (r.get("page_age") or "")[:10]
            meta = f" ({age})" if age else ""
            lines.append(f"- **{title}**{meta} ({url})\n  {desc}")
        return "\n\n".join(lines) or "*No results.*"

    def fetch_insights(self, topic: str, days: int) -> ProviderResult:
        """Fetch structured results from Brave's independent index."""
        self._log_start()

        if not self.api_key:
            return self._create_error_result("BRAVE_API_KEY not set")

        try:
            sources: list[Source] = []
            seen_urls: set[str] = set()
            meta: dict[str, Any] = {"queries": {}}
            sections: list[str] = []
            ran_one = False

            if "social" in self.sources:
                self._log_query("social")
                data = self._search(
                    f"{topic} discussion forum reddit", days, "discussions,web"
                )
                results = self._extract_results(data)
                sources.extend(
                    self._results_to_sources(results, SourceType.SOCIAL, seen_urls)
                )
                meta["queries"]["social"] = {"results_count": len(results)}
                sections.append(
                    f"# Social Media Analysis\n\n{self._format_results(results)}"
                )
                ran_one = True

            if "web" in self.sources:
                if ran_one:
                    time.sleep(_PACE_SECONDS)  # starter-tier 1 req/s ceiling
                self._log_query("web")
                data = self._search(topic, days, "web")
                results = self._extract_results(data)
                sources.extend(
                    self._results_to_sources(results, SourceType.WEB, seen_urls)
                )
                meta["queries"]["web"] = {"results_count": len(results)}
                sections.append(
                    f"# Web Research Analysis\n\n{self._format_results(results)}"
                )

            combined_text = "\n\n---\n\n".join(sections)
            self._log_done(len(sources), len(combined_text))

            return ProviderResult(
                provider=self.provider_name,
                model=self.model_name,
                raw_text=combined_text,
                sources=sources,
                meta=meta,
            )
        except Exception as e:
            return self._create_error_result(f"Brave API error: {e}")

    def summarize_result(self, raw_text: str, topic: str) -> str:
        """Brave is a search API (no LLM): return a truncated result list."""
        return raw_text[:2000] + "..." if len(raw_text) > 2000 else raw_text
