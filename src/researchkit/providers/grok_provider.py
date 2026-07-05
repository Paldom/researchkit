"""xAI Grok 4.1 provider with web search and X search capabilities."""

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

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
)

logger = logging.getLogger(__name__)

# Curated 5-domain allow-list for the Grok "social" query (the xAI web_search
# tool caps allowed_domains at 5). Matches the query's prompt — Reddit + Hacker
# News + discussion communities — and deliberately omits X/Twitter, which the
# dedicated x_search query already covers. (Review M5.)
_GROK_SOCIAL_DOMAINS = [
    "reddit.com",
    "news.ycombinator.com",
    "quora.com",
    "stackoverflow.com",
    "youtube.com",
]


class GrokProvider(BaseProvider):
    """
    xAI Grok 4.1 provider with agentic search capabilities.

    Uses the xAI SDK with:
    - x_search: X/Twitter search with image/video understanding
    - web_search: Web search with domain filtering
    """

    provider_name = "grok"
    model_name = "grok-4-1-fast"

    def __init__(
        self,
        api_key: str | None = None,
        sources: set[str] | None = None,
        model: str | None = None,
    ) -> None:
        """
        Initialize the Grok provider.

        Args:
            api_key: xAI API key (defaults to XAI_API_KEY env var)
            sources: Set of sources to query ("social", "web", or both)
            model: Model to use (overrides default grok-4-1-fast)
        """
        self.api_key = api_key or os.getenv("XAI_API_KEY")
        self.sources = sources or {"social", "web"}
        if model:
            self.model_name = model
        self._client: Any = None

    def _get_client(self) -> Any:
        """Lazy-load the xAI client."""
        if self._client is None:
            try:
                from xai_sdk import Client
            except ImportError as e:
                raise RuntimeError(
                    "xai-sdk package not installed. Run: pip install xai-sdk"
                ) from e
            self._client = Client(api_key=self.api_key, timeout=provider_http_timeout())
        return self._client

    def _serialize_citation(self, citation: Any) -> dict[str, Any]:
        """Convert a citation object to a JSON-serializable dict."""
        if isinstance(citation, str):
            return {"url": citation}
        elif isinstance(citation, dict):
            return citation
        elif hasattr(citation, "url"):
            return {
                "url": getattr(citation, "url", None),
                "title": getattr(citation, "title", None),
            }
        else:
            return {"raw": str(citation)}

    def _serialize_citations(self, citations: list[Any] | None) -> list[dict[str, Any]]:
        """Convert a list of citations to JSON-serializable format."""
        if not citations:
            return []
        return [self._serialize_citation(c) for c in citations]

    def _extract_citations(
        self, citations: list[Any] | None, source_type: SourceType
    ) -> list[Source]:
        """Extract normalized Source objects from xAI citations."""
        sources = []
        if not citations:
            return sources

        for citation in citations:
            url = None
            title = None

            if isinstance(citation, str):
                url = citation
            elif hasattr(citation, "url"):
                url = citation.url
                title = getattr(citation, "title", None)
            elif isinstance(citation, dict):
                url = citation.get("url")
                title = citation.get("title")

            if url:
                sources.append(Source(url=url, title=title, source_type=source_type))

        return sources

    def _run_x_query(
        self,
        topic: str,
        days: int,
        *,
        json_schema: dict[str, Any] | None = None,
    ) -> tuple[str, list[Any]]:
        """Run X/Twitter search query with image and video understanding.

        Args:
            topic: The topic to search for
            days: Number of days to look back
            json_schema: Optional JSON schema for structured output (used in prompt)

        Returns:
            Tuple of (response_text, citations)
        """
        from xai_sdk.chat import user
        from xai_sdk.tools import x_search

        to_date = dt.datetime.now(dt.UTC)
        from_date = to_date - dt.timedelta(days=days)

        tools = [
            x_search(
                from_date=from_date,
                to_date=to_date,
                enable_image_understanding=True,
                enable_video_understanding=True,
            ),
        ]

        enhanced_prompt = f"""{get_user_prompt(topic, days)}

IMPORTANT: You have access to real-time X/Twitter data. Please focus on:
1. Recent X/Twitter posts and discussions about this topic
2. Trending hashtags and conversations
3. Notable accounts discussing this topic
4. Viral posts, threads, images, and videos

Focus on content from {from_date.strftime("%Y-%m-%d")} to {to_date.strftime("%Y-%m-%d")}."""

        # Add JSON schema instruction to prompt if provided
        # (xAI SDK doesn't support response_format like OpenAI)
        if json_schema is not None:
            import json

            enhanced_prompt += f"\n\nIMPORTANT: Return your response as valid JSON matching this schema:\n```json\n{json.dumps(json_schema, indent=2)}\n```\nReturn ONLY the JSON, no other text."

        chat = self._client.chat.create(
            model=self.model_name,
            tools=tools,
            include=["inline_citations"],
        )
        chat.append(user(f"{get_base_system_prompt(days)}\n\n{enhanced_prompt}"))
        response = with_network_retry(
            chat.sample, label="grok.chat.sample:x_search", provider=self.provider_name
        )

        text = response.content or ""
        citations = list(response.citations) if response.citations else []

        return text, citations

    def _run_social_domains_query(
        self,
        topic: str,
        days: int,
        *,
        json_schema: dict[str, Any] | None = None,
    ) -> tuple[str, list[Any]]:
        """Run web search on social domains (Reddit, HN, etc.).

        Args:
            topic: The topic to search for
            days: Number of days to look back
            json_schema: Optional JSON schema for structured output (used in prompt)

        Returns:
            Tuple of (response_text, citations)
        """
        from xai_sdk.chat import user
        from xai_sdk.tools import web_search

        to_date = dt.datetime.now(dt.UTC)
        from_date = to_date - dt.timedelta(days=days)

        # The xAI web_search tool allows at most 5 domains. SOCIAL_DOMAINS[:5]
        # was reddit/old.reddit/x/twitter/tiktok — it omitted the Hacker News the
        # prompt below explicitly asks for, and wasted 2 slots on X/Twitter which
        # the dedicated x_search query already covers. Use a curated list that
        # matches the prompt. (Review M5.)
        tools = [
            web_search(
                allowed_domains=_GROK_SOCIAL_DOMAINS,
                enable_image_understanding=True,
            ),
        ]

        enhanced_prompt = f"""{get_user_prompt(topic, days)}

IMPORTANT: Search social platforms (Reddit, Hacker News, etc.) for discussions about this topic. Please focus on:
1. Reddit threads and discussions
2. Hacker News posts and comments
3. Other social platform discussions
4. Community sentiment and opinions

Focus on content from {from_date.strftime("%Y-%m-%d")} to {to_date.strftime("%Y-%m-%d")}."""

        # Add JSON schema instruction to prompt if provided
        # (xAI SDK doesn't support response_format like OpenAI)
        if json_schema is not None:
            import json

            enhanced_prompt += f"\n\nIMPORTANT: Return your response as valid JSON matching this schema:\n```json\n{json.dumps(json_schema, indent=2)}\n```\nReturn ONLY the JSON, no other text."

        chat = self._client.chat.create(
            model=self.model_name,
            tools=tools,
            include=["inline_citations"],
        )
        chat.append(user(f"{get_base_system_prompt(days)}\n\n{enhanced_prompt}"))
        response = with_network_retry(
            chat.sample,
            label="grok.chat.sample:social_domains",
            provider=self.provider_name,
        )

        text = response.content or ""
        citations = list(response.citations) if response.citations else []

        return text, citations

    def _run_web_query(
        self,
        topic: str,
        days: int,
        *,
        json_schema: dict[str, Any] | None = None,
    ) -> tuple[str, list[Any]]:
        """Run general web research query.

        Args:
            topic: The topic to search for
            days: Number of days to look back
            json_schema: Optional JSON schema for structured output (used in prompt)

        Returns:
            Tuple of (response_text, citations)
        """
        from xai_sdk.chat import user
        from xai_sdk.tools import web_search

        tools = [
            web_search(
                enable_image_understanding=True,
            ),
        ]

        user_prompt = get_web_user_prompt(topic, days)

        # Add JSON schema instruction to prompt if provided
        # (xAI SDK doesn't support response_format like OpenAI)
        if json_schema is not None:
            import json

            user_prompt += f"\n\nIMPORTANT: Return your response as valid JSON matching this schema:\n```json\n{json.dumps(json_schema, indent=2)}\n```\nReturn ONLY the JSON, no other text."

        chat = self._client.chat.create(
            model=self.model_name,
            tools=tools,
            include=["inline_citations"],
        )
        chat.append(user(f"{get_web_system_prompt(days)}\n\n{user_prompt}"))
        response = with_network_retry(
            chat.sample, label="grok.chat.sample:web", provider=self.provider_name
        )

        text = response.content or ""
        citations = list(response.citations) if response.citations else []

        return text, citations

    def fetch_insights(
        self,
        topic: str,
        days: int,
    ) -> ProviderResult:
        """Fetch insights based on configured sources."""
        self._log_start()

        if not self.api_key:
            return self._create_error_result("XAI_API_KEY not set")

        try:
            self._get_client()
        except RuntimeError as e:
            return self._create_error_result(str(e))

        try:
            sources: list[Source] = []
            meta: dict[str, Any] = {}
            sections: list[str] = []

            # X/Twitter search
            if "social" in self.sources:
                self._log_query("x_search")
                x_text, x_citations = self._run_x_query(topic, days)
                sources.extend(self._extract_citations(x_citations, SourceType.SOCIAL))
                meta["x_citations"] = self._serialize_citations(x_citations)
                sections.append(f"# X/Twitter Analysis\n\n{x_text}")

            # Social domains web search (Reddit, HN, etc.)
            if "social" in self.sources:
                self._log_query("social_domains")
                social_text, social_citations = self._run_social_domains_query(
                    topic, days
                )
                sources.extend(
                    self._extract_citations(social_citations, SourceType.SOCIAL)
                )
                meta["social_domain_citations"] = self._serialize_citations(
                    social_citations
                )
                sections.append(f"# Social Platforms Analysis\n\n{social_text}")

            # Web research query
            if "web" in self.sources:
                self._log_query("web")
                web_text, web_citations = self._run_web_query(topic, days)
                sources.extend(self._extract_citations(web_citations, SourceType.WEB))
                meta["web_citations"] = self._serialize_citations(web_citations)
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
            return self._create_error_result(f"Grok API error: {e}")

    def generate_keywords(self, topic: str, days: int, context: str = "") -> list[str]:
        """Generate keywords using Grok (no search tools)."""
        if not self.api_key:
            return []
        try:
            from xai_sdk.chat import user

            from researchkit.keyword_synthesizer import parse_keyword_json
            from researchkit.prompts import (
                get_keyword_generation_system_prompt,
                get_keyword_generation_user_prompt,
            )

            self._get_client()  # Ensure client is initialized
            prompt = (
                get_keyword_generation_system_prompt()
                + "\n\n"
                + get_keyword_generation_user_prompt(topic, days, context)
            )
            chat = self._client.chat.create(model=self.model_name)
            chat.append(user(prompt))
            response = with_network_retry(
                chat.sample,
                label="grok.chat.sample:keywords",
                provider=self.provider_name,
            )
            return parse_keyword_json(response.content or "")
        except Exception as e:
            logger.warning(f"Grok keyword generation failed: {e}")
            return []

    def summarize_result(self, raw_text: str, topic: str) -> str:
        """
        Summarize result using Grok.

        Args:
            raw_text: The raw text to summarize
            topic: The research topic for context

        Returns:
            Summarized text as markdown bullet points
        """
        if not self.api_key:
            return raw_text[:500] + "..." if len(raw_text) > 500 else raw_text

        try:
            from xai_sdk.chat import user

            self._get_client()  # Ensure client is initialized

            prompt = f"""You are a precise summarizer. Your task is to distill social insight reports into their essential points.

Rules:
- Extract 5-8 key bullet points
- Preserve specific examples, quotes, or data points
- Keep platform/source attributions
- Be concise but preserve critical details

Summarize this social insight report into 5-8 key bullet points:

**Topic:** {topic}

---
{raw_text}
---

Format as a markdown bullet list. Start each bullet with a bold label when appropriate (e.g., **Trend:**, **Sentiment:**, **Notable:**)."""

            chat = self._client.chat.create(model=self.model_name)
            chat.append(user(prompt))
            response = with_network_retry(
                chat.sample,
                label="grok.chat.sample:summarize",
                provider=self.provider_name,
            )

            return response.content or ""

        except Exception as e:
            return f"*Summarization failed: {e}*\n\n{raw_text[:500]}..."
