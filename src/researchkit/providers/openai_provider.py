"""OpenAI GPT-5.1 provider with web search capabilities."""

from __future__ import annotations

import logging
import os
from typing import Any

from openai import OpenAI

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
)

logger = logging.getLogger(__name__)


class OpenAIProvider(BaseProvider):
    """
    OpenAI GPT-5.1 provider using the Responses API with web_search tool.

    Runs queries for social media and/or web research based on sources config.
    """

    provider_name = "openai"
    model_name = "gpt-5.5"

    def __init__(
        self,
        api_key: str | None = None,
        sources: set[str] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        """
        Initialize the OpenAI provider.

        Args:
            api_key: OpenAI API key (defaults to OPENAI_API_KEY env var)
            sources: Set of sources to query ("social", "web", or both)
            model: Model to use (overrides default gpt-5.5)
            reasoning_effort: Reasoning effort level ("low", "medium", "high")
        """
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.sources = sources or {"social", "web"}
        if model:
            self.model_name = model
        self.reasoning_effort = reasoning_effort
        self._client: Any = None

    def _get_client(self) -> OpenAI:
        """Lazy-load the OpenAI client.

        ``max_retries=0`` disables the SDK's own retry loop so the unified
        ``network_retry`` policy is the single source of truth. An explicit
        ``timeout`` keeps requests from hanging forever on a stalled socket.
        """
        if self._client is None:
            self._client = OpenAI(
                api_key=self.api_key, max_retries=0, timeout=provider_http_timeout()
            )
        return self._client

    def _serialize_annotation(self, ann: Any) -> dict[str, Any]:
        """Convert an annotation object to a JSON-serializable dict."""
        result: dict[str, Any] = {}

        if hasattr(ann, "url_citation") and ann.url_citation:
            result["url"] = getattr(ann.url_citation, "url", None)
            result["title"] = getattr(ann.url_citation, "title", None)
            result["type"] = "url_citation"
        elif hasattr(ann, "url") and ann.url:
            result["url"] = ann.url
            result["title"] = getattr(ann, "title", None)
            result["type"] = "url"
        elif isinstance(ann, dict):
            return ann
        else:
            # Fallback: try to convert to string
            result["raw"] = str(ann)

        return result

    def _extract_sources(
        self, annotations: list[Any], source_type: SourceType
    ) -> list[Source]:
        """Extract normalized Source objects from OpenAI annotations."""
        sources = []
        seen: set[str] = set()  # dedup URLs across social+web queries (review L22)
        for ann in annotations:
            url = None
            title = None

            if hasattr(ann, "url_citation") and ann.url_citation:
                url = getattr(ann.url_citation, "url", None)
                title = getattr(ann.url_citation, "title", None)
            elif hasattr(ann, "url") and ann.url:
                url = ann.url
                title = getattr(ann, "title", None)
            elif isinstance(ann, dict):
                if "url_citation" in ann:
                    url = ann["url_citation"].get("url")
                    title = ann["url_citation"].get("title")
                elif "url" in ann:
                    url = ann.get("url")
                    title = ann.get("title")

            if url and url not in seen:
                seen.add(url)
                sources.append(Source(url=url, title=title, source_type=source_type))

        return sources

    def _serialize_annotations(self, annotations: list[Any]) -> list[dict[str, Any]]:
        """Convert a list of annotations to JSON-serializable format."""
        return [self._serialize_annotation(ann) for ann in annotations]

    def _run_query(
        self,
        client: OpenAI,
        system_prompt: str,
        user_prompt: str,
        allowed_domains: list[str] | None = None,
        *,
        json_schema: dict[str, Any] | None = None,
        schema_name: str = "response_schema",
        strict: bool = True,
    ) -> tuple[str, list[Any]]:
        """
        Run a single OpenAI query with web search.

        Args:
            client: OpenAI client
            system_prompt: System prompt
            user_prompt: User prompt
            allowed_domains: Optional domain filter for web search
            json_schema: Optional JSON schema for structured output
            schema_name: Name for the schema (used in response_format)
            strict: Whether to enforce strict schema validation

        Returns:
            Tuple of (response_text, annotations)
        """
        tool_config: dict[str, Any] = {
            "type": "web_search",
            "search_context_size": "high",
            "user_location": {"type": "approximate"},
        }

        if allowed_domains:
            tool_config["filters"] = {"allowed_domains": allowed_domains}

        # Build request kwargs
        request_kwargs: dict[str, Any] = {
            "model": self.model_name,
            "input": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "tools": [tool_config],
            "store": False,
        }

        # Add reasoning effort if configured
        if self.reasoning_effort:
            request_kwargs["reasoning"] = {"effort": self.reasoning_effort}

        # Add structured output if json_schema provided
        if json_schema is not None:
            request_kwargs["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "schema": json_schema,
                    "strict": strict,
                }
            }

        response = with_network_retry(
            client.responses.create,
            label="openai.responses.create",
            provider=self.provider_name,
            **request_kwargs,
        )

        # Extract text
        text = getattr(response, "output_text", None)
        if text is None and hasattr(response, "output") and response.output:
            for item in response.output:
                if hasattr(item, "content") and item.content:
                    for content_item in item.content:
                        if hasattr(content_item, "text"):
                            text = content_item.text
                            break
                if text:
                    break

        if not text:
            text = str(response)

        # Extract annotations
        annotations: list[Any] = []
        if hasattr(response, "output") and response.output:
            for item in response.output:
                if hasattr(item, "content") and item.content:
                    for content_item in item.content:
                        if (
                            hasattr(content_item, "annotations")
                            and content_item.annotations
                        ):
                            annotations = list(content_item.annotations)

        return text, annotations

    def fetch_insights(
        self,
        topic: str,
        days: int,
    ) -> ProviderResult:
        """Fetch insights based on configured sources."""
        self._log_start()

        if not self.api_key:
            return self._create_error_result("OPENAI_API_KEY not set")

        try:
            client = self._get_client()
        except RuntimeError as e:
            return self._create_error_result(str(e))

        try:
            sources: list[Source] = []
            meta: dict[str, Any] = {}
            sections: list[str] = []

            # Social media query
            if "social" in self.sources:
                self._log_query("social")
                social_text, social_annotations = self._run_query(
                    client=client,
                    system_prompt=get_base_system_prompt(days),
                    user_prompt=get_user_prompt(topic, days),
                    allowed_domains=SOCIAL_DOMAINS,
                )
                sources.extend(
                    self._extract_sources(social_annotations, SourceType.SOCIAL)
                )
                meta["social_annotations"] = self._serialize_annotations(
                    social_annotations
                )
                sections.append(f"# Social Media Analysis\n\n{social_text}")

            # Web research query
            if "web" in self.sources:
                self._log_query("web")
                web_text, web_annotations = self._run_query(
                    client=client,
                    system_prompt=get_web_system_prompt(days),
                    user_prompt=get_web_user_prompt(topic, days),
                )
                sources.extend(self._extract_sources(web_annotations, SourceType.WEB))
                meta["web_annotations"] = self._serialize_annotations(web_annotations)
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
            return self._create_error_result(f"OpenAI API error: {e}")

    def generate_keywords(self, topic: str, days: int, context: str = "") -> list[str]:
        """Generate keywords using OpenAI chat completions (no web search)."""
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
                label="openai.chat.completions.create:keywords",
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
            logger.warning(f"OpenAI keyword generation failed: {e}")
            return []

    def summarize_result(self, raw_text: str, topic: str) -> str:
        """
        Summarize result using OpenAI.

        Args:
            raw_text: The raw text to summarize
            topic: The research topic for context

        Returns:
            Summarized text as markdown bullet points
        """
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
                label="openai.chat.completions.create:summarize",
                provider=self.provider_name,
                model=self.model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_completion_tokens=1000,
            )
            return response.choices[0].message.content or ""

        except Exception as e:
            return f"*Summarization failed: {e}*\n\n{raw_text[:500]}..."
