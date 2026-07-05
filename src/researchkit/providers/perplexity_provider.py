"""Perplexity Sonar Pro provider with Pro Search capabilities."""

from __future__ import annotations

import os
from typing import Any

from perplexity import Perplexity

from researchkit.network_retry import with_network_retry
from researchkit.providers.base import (
    SOCIAL_DOMAINS,
    BaseProvider,
    ProviderResult,
    Source,
    SourceType,
    get_base_system_prompt,
    get_user_prompt,
    get_web_system_prompt,
    get_web_user_prompt,
    provider_http_timeout,
    recency_to_perplexity_filter,
)


class PerplexityProvider(BaseProvider):
    """
    Perplexity Sonar Pro provider with Pro Search.

    Runs dual queries for social media and web research analysis.
    Supports streaming Pro Search for enhanced results with reasoning steps.
    """

    provider_name = "perplexity"
    model_name = "sonar-pro"

    def __init__(
        self,
        api_key: str | None = None,
        sources: set[str] | None = None,
        model: str | None = None,
        search_type: str = "pro",
    ) -> None:
        """
        Initialize the Perplexity provider.

        Args:
            api_key: Perplexity API key (defaults to PERPLEXITY_API_KEY env var)
            sources: Set of sources to query ("social", "web", or both)
            model: Model to use (overrides default sonar-pro)
            search_type: Search type - "fast", "auto", or "pro" (default: pro)
        """
        self.api_key = api_key or os.getenv("PERPLEXITY_API_KEY")
        self.sources = sources or {"social", "web"}
        if model:
            self.model_name = model
        self.search_type = (search_type or "pro").lower()
        if self.search_type not in {"fast", "auto", "pro"}:
            self.search_type = "pro"
        self._client: Any = None

    def _get_client(self) -> Perplexity:
        """Lazy-load the Perplexity client."""
        if self._client is None:
            # max_retries=0 defers retries to the unified network_retry policy;
            # an explicit timeout prevents hangs on a stalled Pro Search stream.
            timeout = provider_http_timeout()
            if self.api_key:
                self._client = Perplexity(
                    api_key=self.api_key, timeout=timeout, max_retries=0
                )
            else:
                self._client = Perplexity(timeout=timeout, max_retries=0)
        return self._client

    def _as_dict(self, obj: Any) -> dict[str, Any]:
        """Convert pydantic models, dicts, or objects to dict."""
        if obj is None:
            return {}
        if isinstance(obj, dict):
            return obj
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        if hasattr(obj, "dict"):
            return obj.dict()
        # Extract known fields from object
        result = {}
        for k in ("url", "title", "snippet", "date", "last_updated", "source"):
            if hasattr(obj, k):
                result[k] = getattr(obj, k)
        return result

    def _norm_item(self, item: Any) -> dict[str, Any]:
        """Normalize a search result item to standard dict."""
        d = self._as_dict(item)
        return {
            "url": d.get("url"),
            "title": d.get("title"),
            "snippet": d.get("snippet"),
            "date": d.get("date"),
            "last_updated": d.get("last_updated"),
            "source": d.get("source"),
        }

    def _dedupe_by_url(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Deduplicate items by URL."""
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for it in items:
            url = (it.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            out.append(it)
        return out

    def _items_to_sources(
        self, items: list[dict[str, Any]], source_type: SourceType
    ) -> list[Source]:
        """Convert normalized items to Source objects."""
        sources = []
        for it in items:
            if not it.get("url"):
                continue
            sources.append(
                Source(
                    url=it["url"],
                    title=it.get("title"),
                    snippet=it.get("snippet"),
                    date=it.get("date"),
                    last_updated=it.get("last_updated"),
                    source_type=source_type,
                )
            )
        return sources

    def _consume_stream(
        self, stream: Any
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        """
        Consume a streaming response and extract content + metadata.

        Pro Search metadata (search_results, reasoning_steps) arrives at the end.
        """
        content_parts: list[str] = []
        last_search_results: list[Any] = []
        last_reasoning_steps: list[Any] = []
        last_usage: Any = None

        for chunk in stream:
            # Extract content delta
            try:
                delta = chunk.choices[0].delta
                piece = getattr(delta, "content", None)
                if piece:
                    content_parts.append(piece)
            except (AttributeError, IndexError):
                pass

            # Extract metadata (arrives in final chunks)
            if getattr(chunk, "search_results", None):
                last_search_results = chunk.search_results
            if getattr(chunk, "reasoning_steps", None):
                last_reasoning_steps = chunk.reasoning_steps
            if getattr(chunk, "usage", None):
                last_usage = chunk.usage

        # Extract URLs from reasoning_steps (web_search + fetch_url_content tools)
        tool_items: list[dict[str, Any]] = []
        for step in last_reasoning_steps or []:
            s = self._as_dict(step)
            stype = s.get("type")
            if stype == "web_search":
                ws = s.get("web_search", {}) or {}
                for r in ws.get("search_results", []) or []:
                    tool_items.append(self._norm_item(r))
            elif stype == "fetch_url_content":
                fc = s.get("fetch_url_content", {}) or {}
                for c in fc.get("contents", []) or []:
                    tool_items.append(self._norm_item(c))

        # Combine top-level search_results with tool results
        top_items = [self._norm_item(r) for r in (last_search_results or [])]
        all_items = self._dedupe_by_url(top_items + tool_items)

        meta = {
            "search_results": all_items,
            "usage": self._as_dict(last_usage) if last_usage else None,
        }
        return "".join(content_parts), all_items, meta

    def _run_query_nonstream(
        self,
        client: Perplexity,
        system_prompt: str,
        user_prompt: str,
        web_search_options: dict[str, Any],
        search_kwargs: dict[str, Any] | None = None,
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        """Run a non-streaming query."""
        response = with_network_retry(
            client.chat.completions.create,
            label="perplexity.chat.completions:nonstream",
            provider=self.provider_name,
            model=self.model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            web_search_options=web_search_options,
            stream=False,
            **(search_kwargs or {}),
        )

        text = response.choices[0].message.content or ""
        raw_results = getattr(response, "search_results", None) or []
        items = self._dedupe_by_url([self._norm_item(r) for r in raw_results])
        meta = {
            "search_results": items,
            "usage": self._as_dict(getattr(response, "usage", None)),
        }
        return text, items, meta

    def _run_query(
        self,
        client: Perplexity,
        system_prompt: str,
        user_prompt: str,
        recency_filter: str,
        domain_filter: list[str] | None = None,
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        """
        Run a Perplexity query with appropriate streaming based on search_type.

        Pro Search only works with streaming enabled.
        """
        web_search_options: dict[str, Any] = {
            "search_context_size": "high",
            "search_type": self.search_type,
        }

        # `search_recency_filter` and `search_domain_filter` are TOP-LEVEL
        # `chat.completions.create` params in the Perplexity SDK, not
        # `web_search_options` fields. Nesting them (the old bug) meant the API
        # silently ignored the lookback window and social-domain restriction, so
        # the "social" query returned stale, off-domain results. (Review M2.)
        search_kwargs: dict[str, Any] = {"search_recency_filter": recency_filter}
        if domain_filter:
            search_kwargs["search_domain_filter"] = domain_filter[:10]

        # Pro/auto search requires streaming
        do_stream = self.search_type in {"auto", "pro"}

        if do_stream:
            # Wrap stream creation + consumption in one retry: if the socket
            # drops mid-stream the whole exchange restarts cleanly.
            def _stream_and_consume() -> tuple[
                str, list[dict[str, Any]], dict[str, Any]
            ]:
                # search_recency_filter/search_domain_filter are valid top-level
                # params in the Perplexity SDK (verified at runtime); mypy's
                # overload resolution can't see them through **kwargs.
                stream = client.chat.completions.create(  # type: ignore[call-overload]
                    model=self.model_name,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    web_search_options=web_search_options,
                    stream=True,
                    **search_kwargs,
                )
                return self._consume_stream(stream)

            return with_network_retry(
                _stream_and_consume,
                label="perplexity.chat.completions:stream",
                provider=self.provider_name,
            )
        else:
            return self._run_query_nonstream(
                client, system_prompt, user_prompt, web_search_options, search_kwargs
            )

    def validate_urls_against_search_results(
        self,
        items: list[dict[str, Any]],
        search_results: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        """Filter items to only include those with URLs found in search results.

        Perplexity can hallucinate URLs when forced to include links in JSON.
        This validates against the actual search_results returned by the API.

        Args:
            items: List of item dicts containing 'url' field
            search_results: List of search result dicts from API

        Returns:
            Filtered list with only valid URLs
        """
        valid_urls = {
            r.get("url", "").lower().rstrip("/") for r in search_results if r.get("url")
        }

        validated = []
        for item in items:
            url = item.get("url", "").lower().rstrip("/")
            if url in valid_urls:
                validated.append(item)

        return validated

    def fetch_insights(
        self,
        topic: str,
        days: int,
    ) -> ProviderResult:
        """Fetch insights based on configured sources."""
        self._log_start()

        if not self.api_key and not os.getenv("PERPLEXITY_API_KEY"):
            return self._create_error_result("PERPLEXITY_API_KEY not set")

        try:
            client = self._get_client()
        except RuntimeError as e:
            return self._create_error_result(str(e))

        try:
            recency_filter = recency_to_perplexity_filter(days)
            sources: list[Source] = []
            meta: dict[str, Any] = {
                "search_recency_filter": recency_filter,
                "search_type": self.search_type,
                "queries": {},
            }
            sections: list[str] = []

            # Social media query
            if "social" in self.sources:
                self._log_query("social")
                social_text, social_items, social_meta = self._run_query(
                    client=client,
                    system_prompt=get_base_system_prompt(days),
                    user_prompt=get_user_prompt(topic, days),
                    recency_filter=recency_filter,
                    domain_filter=SOCIAL_DOMAINS,
                )
                sources.extend(self._items_to_sources(social_items, SourceType.SOCIAL))
                meta["queries"]["social"] = social_meta
                sections.append(f"# Social Media Analysis\n\n{social_text}")

            # Web research query
            if "web" in self.sources:
                self._log_query("web")
                web_text, web_items, web_meta = self._run_query(
                    client=client,
                    system_prompt=get_web_system_prompt(days),
                    user_prompt=get_web_user_prompt(topic, days),
                    recency_filter=recency_filter,
                )
                sources.extend(self._items_to_sources(web_items, SourceType.WEB))
                meta["queries"]["web"] = web_meta
                sections.append(f"# Web Research Analysis\n\n{web_text}")

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
            return self._create_error_result(f"Perplexity API error: {e}")

    def summarize_result(self, raw_text: str, topic: str) -> str:
        """
        Summarize result using Perplexity.

        Args:
            raw_text: The raw text to summarize
            topic: The research topic for context

        Returns:
            Summarized text as markdown bullet points
        """
        if not self.api_key and not os.getenv("PERPLEXITY_API_KEY"):
            return raw_text[:500] + "..." if len(raw_text) > 500 else raw_text

        try:
            client = self._get_client()

            system_prompt = """You are a precise summarizer. Your task is to distill social insight reports into their essential points.

Rules:
- Extract 5-8 key bullet points
- Preserve specific examples, quotes, or data points
- Keep platform/source attributions
- Be concise but preserve critical details"""

            user_prompt = f"""Summarize this social insight report into 5-8 key bullet points:

**Topic:** {topic}

---
{raw_text}
---

Format as a markdown bullet list. Start each bullet with a bold label when appropriate (e.g., **Trend:**, **Sentiment:**, **Notable:**)."""

            response = with_network_retry(
                client.chat.completions.create,
                label="perplexity.chat.completions:summarize",
                provider=self.provider_name,
                model=self.model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                stream=False,
            )

            return response.choices[0].message.content or ""

        except Exception as e:
            return f"*Summarization failed: {e}*\n\n{raw_text[:500]}..."
