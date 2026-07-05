"""z.ai GLM provider with web search.

GLM (Zhipu AI / z.ai) exposes an OpenAI-compatible chat-completions endpoint,
so this provider reuses the OpenAI SDK pointed at the z.ai base URL for the
analysis (synthesis) step.

Citations come from z.ai's **standalone** Web Search API (``POST /web_search``),
NOT from the chat ``web_search`` tool. The OpenAI-compatible chat endpoint runs
web search but fuses the results into the answer text only — it does *not* return
the structured ``web_search`` array in the response body (verified at the raw
HTTP level). The dedicated ``/web_search`` endpoint returns the result objects
(title/link/content/publish_date/...), so we search there first and ground the
chat synthesis on those exact results.

The same module-level helpers (``make_zai_client``, ``is_glm_model``) let other
parts of the app use GLM as a generic OpenAI-compatible model for roles such as
the topic improver or the summarizer.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from openai import OpenAI

from researchkit.network_retry import with_network_retry
from researchkit.providers.base import (
    BaseProvider,
    ProviderResult,
    Source,
    SourceType,
    get_base_system_prompt,
    get_user_prompt,
    get_web_system_prompt,
    get_web_user_prompt,
    provider_http_timeout,
    recency_to_glm_filter,
)

logger = logging.getLogger(__name__)

# OpenAI-compatible base URL for the international z.ai endpoint (trailing slash
# required — the SDK appends ``chat/completions``, and we append ``web_search``).
ZAI_BASE_URL = "https://api.z.ai/api/paas/v4/"

# z.ai's search engine caps results at 50 per query.
GLM_SEARCH_MAX_COUNT = 50

# Default search engine for the Web Search API. ``search-prime`` is the engine
# documented for the international z.ai endpoint (the older ``search_pro_jina``
# id is not honoured here).
GLM_DEFAULT_SEARCH_ENGINE = "search-prime"


def get_zai_api_key(explicit: str | None = None) -> str | None:
    """Resolve the z.ai API key from an explicit value or the environment."""
    return (
        explicit
        or os.getenv("ZAI_API_KEY")
        or os.getenv("GLM_API_KEY")
        or os.getenv("ZHIPUAI_API_KEY")
    )


def is_glm_model(model: str | None) -> bool:
    """Return ``True`` when ``model`` is a GLM / z.ai model identifier."""
    return bool(model) and model.lower().startswith("glm")


def make_zai_client(api_key: str | None = None) -> OpenAI:
    """Create an OpenAI client configured for the z.ai endpoint.

    ``max_retries=0`` defers retries to the unified ``network_retry`` policy and
    an explicit ``timeout`` avoids hanging on a stalled socket — matching the
    other OpenAI-based providers.
    """
    key = get_zai_api_key(api_key)
    if not key:
        raise RuntimeError("ZAI_API_KEY not set")
    return OpenAI(
        api_key=key,
        base_url=ZAI_BASE_URL,
        max_retries=0,
        timeout=provider_http_timeout(),
    )


class GLMProvider(BaseProvider):
    """z.ai GLM provider using chat completions with the ``web_search`` tool.

    Runs queries for social media and/or web research based on ``sources``,
    mirroring the other search providers (analysis text + extracted citations).
    """

    provider_name = "glm"
    model_name = "glm-4.6"

    def __init__(
        self,
        api_key: str | None = None,
        sources: set[str] | None = None,
        model: str | None = None,
        search_count: int = GLM_SEARCH_MAX_COUNT,
        search_engine: str | None = None,
    ) -> None:
        """
        Initialize the GLM provider.

        Args:
            api_key: z.ai API key (defaults to ZAI_API_KEY env var)
            sources: Set of sources to query ("social", "web", or both)
            model: Model to use (overrides default glm-4.6)
            search_count: Number of web-search results per query (capped at 50)
            search_engine: z.ai search engine (default ``search_pro_jina``)
        """
        self.api_key = get_zai_api_key(api_key)
        self.sources = sources or {"social", "web"}
        if model:
            self.model_name = model
        self.search_count = max(1, min(int(search_count), GLM_SEARCH_MAX_COUNT))
        self.search_engine = search_engine or GLM_DEFAULT_SEARCH_ENGINE
        self._client: Any = None

    def _get_client(self) -> OpenAI:
        """Lazy-load the z.ai (OpenAI-compatible) client."""
        if self._client is None:
            self._client = make_zai_client(self.api_key)
        return self._client

    @staticmethod
    def _as_dict(item: Any) -> dict[str, Any]:
        """Coerce a web_search result item to a plain dict."""
        if isinstance(item, dict):
            return item
        if hasattr(item, "model_dump"):
            try:
                return item.model_dump()
            except Exception:
                pass
        return {
            k: getattr(item, k, None)
            for k in ("title", "content", "link", "url", "media", "publish_date")
            if hasattr(item, k)
        }

    def _web_search_api(self, query: str, days: int) -> list[dict[str, Any]]:
        """Fetch structured results from z.ai's standalone Web Search API.

        Returns the raw ``search_result`` objects (title/link/content/...). This
        is the only z.ai surface that exposes structured citations — the chat
        ``web_search`` tool fuses results into text without returning the array.
        """
        payload = {
            "search_engine": self.search_engine,
            "search_query": query[:500],
            "count": self.search_count,
            "search_recency_filter": recency_to_glm_filter(days),
        }

        def _call() -> httpx.Response:
            resp = httpx.post(
                f"{ZAI_BASE_URL}web_search",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=provider_http_timeout(),
            )
            resp.raise_for_status()
            return resp

        response = with_network_retry(
            _call, label="glm.web_search", provider=self.provider_name
        )
        data = response.json()
        items = data.get("search_result") or []
        return [self._as_dict(it) for it in items]

    @staticmethod
    def _format_sources_context(items: list[dict[str, Any]]) -> str:
        """Render search results as a numbered, citeable context block."""
        blocks: list[str] = []
        for i, it in enumerate(items, 1):
            title = it.get("title") or ""
            link = it.get("link") or it.get("url") or ""
            content = (it.get("content") or "").strip()
            date = it.get("publish_date") or ""
            meta = f" ({date})" if date else ""
            blocks.append(f"[{i}] {title}{meta}\n{link}\n{content}")
        return "\n\n".join(blocks)

    def _extract_sources(
        self,
        web_items: list[dict[str, Any]],
        source_type: SourceType,
        seen: set[str] | None = None,
    ) -> list[Source]:
        """Map z.ai web_search results to normalized Source objects.

        Pass a shared ``seen`` set across the social + web queries to dedup a URL
        returned by both (review S8). Sibling providers (Tavily, Codex) already do.
        """
        if seen is None:
            seen = set()
        sources: list[Source] = []
        for it in web_items:
            url = it.get("link") or it.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            sources.append(
                Source(
                    url=url,
                    title=it.get("title"),
                    snippet=it.get("content"),
                    date=it.get("publish_date"),
                    source_type=source_type,
                )
            )
        return sources

    def _run_query(
        self,
        client: OpenAI,
        system_prompt: str,
        user_prompt: str,
        days: int,
        label: str,
        search_query: str,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Search via the Web Search API, then synthesize analysis over results.

        Returns the analysis text plus the raw search-result items (used as
        citations). The synthesis is grounded on exactly the fetched results so
        the cited sources match the analysis.
        """
        web_items = self._web_search_api(search_query, days)

        if web_items:
            context = self._format_sources_context(web_items)
            grounded_prompt = (
                f"{user_prompt}\n\n"
                "Base your analysis ONLY on the web search results below and cite "
                "them inline as [n]. Do not invent sources.\n\n"
                f"## Web Search Results\n\n{context}"
            )
        else:
            grounded_prompt = user_prompt

        response = with_network_retry(
            client.chat.completions.create,
            label=label,
            provider=self.provider_name,
            model=self.model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": grounded_prompt},
            ],
        )

        text = ""
        if getattr(response, "choices", None):
            text = response.choices[0].message.content or ""
        return text, web_items

    def fetch_insights(
        self,
        topic: str,
        days: int,
    ) -> ProviderResult:
        """Fetch insights based on configured sources."""
        self._log_start()

        if not self.api_key:
            return self._create_error_result("ZAI_API_KEY not set")

        try:
            client = self._get_client()
        except RuntimeError as e:
            return self._create_error_result(str(e))

        try:
            sources: list[Source] = []
            seen_urls: set[str] = set()  # dedup across social + web (review S8)
            meta: dict[str, Any] = {
                "search_recency_filter": recency_to_glm_filter(days),
                "search_engine": self.search_engine,
                "search_count": self.search_count,
            }
            sections: list[str] = []

            # Social media query
            if "social" in self.sources:
                self._log_query("social")
                social_text, social_items = self._run_query(
                    client=client,
                    system_prompt=get_base_system_prompt(days),
                    user_prompt=get_user_prompt(topic, days),
                    days=days,
                    label="glm.chat.completions:social",
                    search_query=f"{topic} social media discussion reddit twitter opinions",
                )
                sources.extend(
                    self._extract_sources(social_items, SourceType.SOCIAL, seen_urls)
                )
                meta["social_results"] = social_items
                sections.append(f"# Social Media Analysis\n\n{social_text}")

            # Web research query
            if "web" in self.sources:
                self._log_query("web")
                web_text, web_items = self._run_query(
                    client=client,
                    system_prompt=get_web_system_prompt(days),
                    user_prompt=get_web_user_prompt(topic, days),
                    days=days,
                    label="glm.chat.completions:web",
                    search_query=topic,
                )
                sources.extend(
                    self._extract_sources(web_items, SourceType.WEB, seen_urls)
                )
                meta["web_results"] = web_items
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
            return self._create_error_result(f"GLM API error: {e}")

    def generate_keywords(self, topic: str, days: int, context: str = "") -> list[str]:
        """Generate keywords using GLM chat completions (no web search)."""
        if not self.api_key:
            return []
        try:
            from researchkit.keyword_synthesizer import parse_keyword_json
            from researchkit.prompts import (
                get_keyword_generation_system_prompt,
                get_keyword_generation_user_prompt,
            )

            client = self._get_client()
            response = with_network_retry(
                client.chat.completions.create,
                label="glm.chat.completions:keywords",
                provider=self.provider_name,
                model=self.model_name,
                messages=[
                    {
                        "role": "system",
                        "content": get_keyword_generation_system_prompt(),
                    },
                    {
                        "role": "user",
                        "content": get_keyword_generation_user_prompt(
                            topic, days, context
                        ),
                    },
                ],
                response_format={"type": "json_object"},
            )
            return parse_keyword_json(response.choices[0].message.content or "")
        except Exception as e:
            logger.warning(f"GLM keyword generation failed: {e}")
            return []

    def summarize_result(self, raw_text: str, topic: str) -> str:
        """Summarize this provider's result using GLM (no web search)."""
        if not self.api_key:
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
                label="glm.chat.completions:summarize",
                provider=self.provider_name,
                model=self.model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=1000,
            )
            return response.choices[0].message.content or ""

        except Exception as e:
            return f"*Summarization failed: {e}*\n\n{raw_text[:500]}..."
