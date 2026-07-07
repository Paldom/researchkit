"""Exa search provider for web and social research.

Exa is an embeddings-first (neural) search API: strongest where the query
shares no keywords with the target content. Like Tavily, it is a search
provider rather than an LLM — the topic is the query and results become
cited sources directly. (Exa additionally powers a site-research connector
for deep per-item summarization; the two are independent.)
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Any
from urllib.parse import urlsplit

from researchkit.network_retry import with_network_retry
from researchkit.providers.base import (
    SOCIAL_DOMAINS,
    BaseProvider,
    ProviderResult,
    Source,
    SourceType,
)


class ExaProvider(BaseProvider):
    """
    Exa neural-search provider.

    Runs the topic through Exa's search-and-contents API — once filtered to
    social/discussion domains, once for the broader web — and returns the
    results as cited sources with highlight snippets.
    """

    provider_name = "exa"
    model_name = "exa-search"

    def __init__(
        self,
        api_key: str | None = None,
        sources: set[str] | None = None,
        model: str | None = None,
        num_results: int = 20,
    ) -> None:
        """
        Initialize the Exa provider.

        Args:
            api_key: Exa API key (defaults to EXA_API_KEY env var)
            sources: Set of sources to query ("social", "web", or both)
            model: Display model name (overrides default)
            num_results: Results per query (max 100)
        """
        self.api_key = api_key or os.getenv("EXA_API_KEY")
        self.sources = sources or {"social", "web"}
        if model:
            self.model_name = model
        self.num_results = max(1, min(int(num_results), 100))
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            from exa_py import Exa

            self._client = Exa(api_key=self.api_key)
        return self._client

    def _search(
        self,
        topic: str,
        days: int,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
    ) -> Any:
        start = (dt.datetime.now(dt.UTC) - dt.timedelta(days=days)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
        kwargs: dict[str, Any] = {
            "num_results": self.num_results,
            "start_published_date": start,
            "highlights": {"num_sentences": 2, "highlights_per_url": 2},
        }
        if include_domains:
            kwargs["include_domains"] = include_domains
        if exclude_domains:
            kwargs["exclude_domains"] = exclude_domains
        return with_network_retry(
            self._get_client().search_and_contents,
            label="exa.provider.search",
            provider=self.provider_name,
            query=topic,
            **kwargs,
        )

    @staticmethod
    def _to_sources(response: Any, source_type: SourceType) -> list[Source]:
        sources: list[Source] = []
        seen: set[str] = set()
        for r in getattr(response, "results", []) or []:
            url = (getattr(r, "url", "") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            highlights = getattr(r, "highlights", None) or []
            sources.append(
                Source(
                    url=url,
                    title=getattr(r, "title", None),
                    snippet=" … ".join(highlights)[:500] or None,
                    date=(getattr(r, "published_date", None) or None),
                    source_type=source_type,
                )
            )
        return sources

    @staticmethod
    def _section(label: str, sources: list[Source]) -> str:
        lines = [f"# {label}", ""]
        for s in sources:
            host = urlsplit(s.url).netloc
            lines.append(f"- **{s.title or host}** — {s.snippet or s.url}")
            lines.append(f"  <{s.url}>")
        return "\n".join(lines)

    def fetch_insights(self, topic: str, days: int) -> ProviderResult:
        """Fetch insights using Exa neural search with the topic as query."""
        self._log_start()

        if not self.api_key:
            return self._create_error_result("EXA_API_KEY not set")

        try:
            sources: list[Source] = []
            sections: list[str] = []
            meta: dict[str, Any] = {"num_results": self.num_results, "queries": {}}

            if "social" in self.sources:
                self._log_query("social")
                resp = self._search(topic, days, include_domains=SOCIAL_DOMAINS)
                social = self._to_sources(resp, SourceType.SOCIAL)
                sources.extend(social)
                meta["queries"]["social"] = {"results_count": len(social)}
                sections.append(self._section("Social Search Results", social))

            if "web" in self.sources:
                self._log_query("web")
                resp = self._search(topic, days, exclude_domains=SOCIAL_DOMAINS)
                web = self._to_sources(resp, SourceType.WEB)
                sources.extend(web)
                meta["queries"]["web"] = {"results_count": len(web)}
                sections.append(self._section("Web Search Results", web))

            combined = "\n\n---\n\n".join(sections)
            self._log_done(len(sources), len(combined))
            return ProviderResult(
                provider=self.provider_name,
                model=self.model_name,
                raw_text=combined,
                sources=sources,
                meta=meta,
            )
        except Exception as e:
            return self._create_error_result(f"Exa error: {e}")
