"""Tavily search provider for web and social research.

Tavily is a search API that returns results directly from the web.
Unlike LLM-based providers, it uses the topic directly as the search query
and returns structured results with an AI-generated answer.
"""

from __future__ import annotations

import os
from typing import Any

import requests

from researchkit.network_retry import with_network_retry
from researchkit.providers.base import (
    SOCIAL_DOMAINS,
    BaseProvider,
    ProviderResult,
    Source,
    SourceType,
)

TAVILY_API_URL = "https://api.tavily.com/search"


class TavilyProvider(BaseProvider):
    """
    Tavily search provider.

    Uses Tavily's search API to find relevant content across social media
    and web sources. Returns AI-generated answers alongside structured
    search results with sources.
    """

    provider_name = "tavily"
    model_name = "tavily-search"

    def __init__(
        self,
        api_key: str | None = None,
        sources: set[str] | None = None,
        model: str | None = None,
        search_depth: str = "advanced",
    ) -> None:
        """
        Initialize the Tavily provider.

        Args:
            api_key: Tavily API key (defaults to TAVILY_API_KEY env var)
            sources: Set of sources to query ("social", "web", or both)
            model: Display model name (overrides default)
            search_depth: Search depth - "basic", "advanced", "fast", "ultra-fast"
                (default: advanced). "advanced" costs 2 credits, others cost 1.
        """
        self.api_key = api_key or os.getenv("TAVILY_API_KEY")
        self.sources = sources or {"social", "web"}
        if model:
            self.model_name = model
        self.search_depth = (search_depth or "advanced").lower()
        if self.search_depth not in {"basic", "advanced", "fast", "ultra-fast"}:
            self.search_depth = "advanced"

    @staticmethod
    def _days_to_time_range(days: int) -> str:
        """Convert days to Tavily time_range filter."""
        if days <= 1:
            return "day"
        elif days <= 7:
            return "week"
        elif days <= 31:
            return "month"
        return "year"

    def _search(
        self,
        query: str,
        days: int,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        topic: str = "general",
        max_results: int = 10,
    ) -> dict[str, Any]:
        """
        Execute a Tavily search.

        Args:
            query: Search query (typically the research topic)
            days: Lookback window for time_range filter
            include_domains: Whitelist domains (max 300)
            exclude_domains: Blacklist domains (max 150)
            topic: Tavily topic filter - "general", "news", "finance"
            max_results: Number of results to return

        Returns:
            Tavily API response dict
        """
        payload: dict[str, Any] = {
            "query": query,
            "search_depth": self.search_depth,
            "max_results": max_results,
            "topic": topic,
            "time_range": self._days_to_time_range(days),
            "include_answer": "advanced",
        }
        if include_domains:
            payload["include_domains"] = include_domains
        if exclude_domains:
            payload["exclude_domains"] = exclude_domains

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        def _post() -> dict[str, Any]:
            resp = requests.post(
                TAVILY_API_URL,
                json=payload,
                headers=headers,
                timeout=(10, 60),
            )
            resp.raise_for_status()
            return resp.json()

        return with_network_retry(
            _post, label="tavily.search", provider=self.provider_name
        )

    @staticmethod
    def _results_to_sources(
        results: list[dict[str, Any]],
        source_type: SourceType,
    ) -> list[Source]:
        """Convert Tavily search results to Source objects."""
        sources: list[Source] = []
        seen_urls: set[str] = set()
        for r in results:
            url = (r.get("url") or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            sources.append(
                Source(
                    url=url,
                    title=r.get("title"),
                    snippet=r.get("content"),
                    source_type=source_type,
                )
            )
        return sources

    @staticmethod
    def _format_results_fallback(results: list[dict[str, Any]]) -> str:
        """Format results as markdown when no answer is available."""
        lines: list[str] = []
        for r in results:
            title = r.get("title", "Untitled")
            content = r.get("content", "")
            url = r.get("url", "")
            lines.append(f"- **{title}** ({url})\n  {content}")
        return "\n\n".join(lines)

    def fetch_insights(self, topic: str, days: int) -> ProviderResult:
        """Fetch insights using Tavily search with the topic as query."""
        self._log_start()

        if not self.api_key:
            return self._create_error_result("TAVILY_API_KEY not set")

        try:
            sources: list[Source] = []
            meta: dict[str, Any] = {
                "search_depth": self.search_depth,
                "queries": {},
            }
            sections: list[str] = []

            # Social media query - filter to social/discussion domains
            if "social" in self.sources:
                self._log_query("social")
                social_resp = self._search(
                    query=topic,
                    days=days,
                    include_domains=SOCIAL_DOMAINS,
                    max_results=20,
                )
                social_results = social_resp.get("results", [])
                social_answer = social_resp.get("answer", "")
                sources.extend(
                    self._results_to_sources(social_results, SourceType.SOCIAL)
                )
                meta["queries"]["social"] = {
                    "results_count": len(social_results),
                    "response_time": social_resp.get("response_time"),
                }

                section = "# Social Media Analysis\n\n"
                if social_answer:
                    section += social_answer
                else:
                    section += self._format_results_fallback(social_results)
                sections.append(section)

            # Web research query - exclude social domains, use news topic
            if "web" in self.sources:
                self._log_query("web")
                web_resp = self._search(
                    query=topic,
                    days=days,
                    exclude_domains=SOCIAL_DOMAINS,
                    topic="news",
                    max_results=20,
                )
                web_results = web_resp.get("results", [])
                web_answer = web_resp.get("answer", "")
                sources.extend(self._results_to_sources(web_results, SourceType.WEB))
                meta["queries"]["web"] = {
                    "results_count": len(web_results),
                    "response_time": web_resp.get("response_time"),
                }

                section = "# Web Research Analysis\n\n"
                if web_answer:
                    section += web_answer
                else:
                    section += self._format_results_fallback(web_results)
                sections.append(section)

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
            return self._create_error_result(f"Tavily API error: {e}")

    def summarize_result(self, raw_text: str, topic: str) -> str:
        """
        Summarize result using a focused Tavily search.

        Since Tavily is a search API (not an LLM), summarization is done by
        running a focused search with include_answer to get a concise summary.

        Args:
            raw_text: The raw text to summarize
            topic: The research topic for context

        Returns:
            Summarized text
        """
        if not self.api_key:
            return raw_text[:500] + "..." if len(raw_text) > 500 else raw_text

        try:
            resp = self._search(
                query=f"latest developments and public discussion about: {topic}",
                days=30,
                max_results=5,
            )
            answer = resp.get("answer", "")
            if answer:
                return answer
        except Exception:
            pass

        return raw_text[:2000] + "..." if len(raw_text) > 2000 else raw_text
