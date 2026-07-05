"""Google Gemini 3 Pro provider with Google Search grounding."""

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

from google import genai
from google.genai import types

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
from researchkit.safe_io import safe_fetch_text

logger = logging.getLogger(__name__)


def _extract_html_title(html: str) -> str | None:
    """Best-effort ``<title>`` extraction from a capped HTML snippet."""
    lower = html.lower()
    start = lower.find("<title>")
    if start == -1:
        return None
    start += len("<title>")
    end = lower.find("</title>", start)
    if end <= start:
        return None
    title = " ".join(html[start:end].split())[:200]
    return title or None


class GeminiProvider(BaseProvider):
    """
    Google Gemini 3 Pro provider with Google Search grounding.

    Runs queries for social media and/or web research based on sources config.
    """

    provider_name = "gemini"
    model_name = "gemini-3.1-pro-preview"

    def __init__(
        self,
        api_key: str | None = None,
        sources: set[str] | None = None,
        model: str | None = None,
    ) -> None:
        """
        Initialize the Gemini provider.

        Args:
            api_key: Google AI API key (defaults to GEMINI_API_KEY or GOOGLE_API_KEY env var)
            sources: Set of sources to query ("social", "web", or both)
            model: Model to use (overrides default gemini-3.1-pro-preview)
        """
        self.api_key = (
            api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        )
        self.sources = sources or {"social", "web"}
        if model:
            self.model_name = model
        self._client: Any = None
        # Cache redirect resolutions so a URL cited in both the social and web
        # queries is fetched once, not per occurrence (review M6).
        self._redirect_cache: dict[str, tuple[str, str | None]] = {}

    def _get_client(self) -> genai.Client:
        """Lazy-load the Gemini client."""
        if self._client is None:
            if not self.api_key:
                raise RuntimeError("GEMINI_API_KEY or GOOGLE_API_KEY not set")
            # genai expects the timeout in milliseconds.
            self._client = genai.Client(
                api_key=self.api_key,
                http_options=types.HttpOptions(
                    timeout=int(provider_http_timeout() * 1000)
                ),
            )
        return self._client

    def _resolve_redirect_url(self, redirect_url: str) -> tuple[str, str | None]:
        """
        Resolve a Gemini grounding redirect URL to get the actual URL + title.

        The Gemini API returns URLs like:
        https://vertexaisearch.cloud.google.com/grounding-api-redirect/...

        Uses a single best-effort, SSRF-guarded, size-capped GET (no retry
        wrapper): a dead cited link must not cost several retries x timeout and
        stall the whole aggregation pipeline. Results are cached so a URL cited in
        both the social and web queries is fetched once. (Review M6.)

        Returns:
            Tuple of (resolved_url, page_title or None)
        """
        if "grounding-api-redirect" not in redirect_url:
            return redirect_url, None

        cached = self._redirect_cache.get(redirect_url)
        if cached is not None:
            return cached

        html, final_url = safe_fetch_text(
            redirect_url,
            timeout=8.0,
            max_bytes=15_000,
            headers={"User-Agent": "Mozilla/5.0 (compatible; SocialResearch/1.0)"},
        )
        resolved = (
            final_url
            if final_url and "grounding-api-redirect" not in final_url
            else redirect_url
        )
        title = _extract_html_title(html) if html else None
        result = (resolved, title)
        self._redirect_cache[redirect_url] = result
        return result

    def _extract_sources(
        self,
        grounding_chunks: list[dict],
        source_type: SourceType,
        seen: set[str] | None = None,
    ) -> list[Source]:
        """Extract normalized Source objects from Gemini grounding chunks.

        Pass a shared ``seen`` set across the social + web queries to avoid
        emitting the same resolved URL twice (review L22).
        """
        if seen is None:
            seen = set()
        sources = []
        for chunk in grounding_chunks:
            if "web" in chunk and chunk["web"].get("uri"):
                redirect_url = chunk["web"]["uri"]
                domain_title = chunk["web"].get("title")  # Usually just domain

                # Resolve redirect to get actual URL
                actual_url, page_title = self._resolve_redirect_url(redirect_url)
                if not actual_url or actual_url in seen:
                    continue
                seen.add(actual_url)

                # Use page title if available, otherwise fall back to domain
                title = page_title or domain_title

                sources.append(
                    Source(
                        url=actual_url,
                        title=title,
                        source_type=source_type,
                    )
                )
        return sources

    def _convert_schema_for_gemini(self, schema: dict[str, Any]) -> dict[str, Any]:
        """
        Convert a JSON Schema to Gemini's expected format.

        Gemini doesn't support 'additionalProperties' field, so we need to remove it.
        """

        def clean_schema(obj: Any) -> Any:
            if isinstance(obj, dict):
                # Remove additionalProperties (Gemini doesn't support it)
                cleaned = {
                    k: clean_schema(v)
                    for k, v in obj.items()
                    if k != "additionalProperties"
                }
                return cleaned
            elif isinstance(obj, list):
                return [clean_schema(item) for item in obj]
            else:
                return obj

        return clean_schema(schema)

    def _run_query(
        self,
        client: genai.Client,
        system_prompt: str,
        user_prompt: str,
        time_range: tuple[dt.datetime, dt.datetime],
        *,
        json_schema: dict[str, Any] | None = None,
    ) -> tuple[str, list[dict]]:
        """
        Run a single Gemini query with Google Search grounding.

        Args:
            client: Gemini client
            system_prompt: System prompt
            user_prompt: User prompt
            time_range: Tuple of (start_time, end_time) for search filter
            json_schema: Optional JSON schema for structured output

        Returns:
            Tuple of (response_text, grounding_chunks)
        """
        full_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"

        # Strip microseconds - Gemini API doesn't support sub-second granularity
        start_time = time_range[0].replace(microsecond=0)
        end_time = time_range[1].replace(microsecond=0)

        google_search_tool = types.Tool(
            google_search=types.GoogleSearch(
                timeRangeFilter=types.Interval(
                    startTime=start_time,
                    endTime=end_time,
                )
            )
        )

        # Build config with optional structured output
        config_kwargs: dict[str, Any] = {
            "tools": [google_search_tool],
            "temperature": 0.7,
        }

        if json_schema is not None:
            config_kwargs["response_mime_type"] = "application/json"
            # Convert schema to Gemini format (remove additionalProperties which Gemini doesn't support)
            config_kwargs["response_schema"] = self._convert_schema_for_gemini(
                json_schema
            )

        response = with_network_retry(
            client.models.generate_content,
            label="gemini.generate_content:research",
            provider=self.provider_name,
            model=self.model_name,
            contents=full_prompt,
            config=types.GenerateContentConfig(**config_kwargs),
        )

        # Extract text
        text = getattr(response, "text", None)
        if text is None and hasattr(response, "candidates") and response.candidates:
            for candidate in response.candidates:
                if (
                    hasattr(candidate, "content")
                    and candidate.content
                    and hasattr(candidate.content, "parts")
                    and candidate.content.parts
                ):
                    for part in candidate.content.parts:
                        if hasattr(part, "text"):
                            text = part.text
                            break
                if text:
                    break

        if not text:
            # An empty/blocked candidate used to fall back to str(response) — a
            # giant object repr dumped into the report and (worse) reported as a
            # successful result. Return empty so the caller treats it as an empty
            # query instead. (Review L23.)
            logger.warning(
                "Gemini returned no candidate text (blocked/empty response)",
                extra={"stage": "provider_query", "provider": self.provider_name},
            )
            text = ""

        # Extract grounding chunks
        grounding_chunks = []
        if hasattr(response, "candidates") and response.candidates:
            for candidate in response.candidates:
                if hasattr(candidate, "grounding_metadata"):
                    grounding = candidate.grounding_metadata
                    if (
                        hasattr(grounding, "grounding_chunks")
                        and grounding.grounding_chunks
                    ):
                        grounding_chunks = [
                            {
                                "web": {
                                    "uri": getattr(chunk.web, "uri", None),
                                    "title": getattr(chunk.web, "title", None),
                                }
                            }
                            for chunk in grounding.grounding_chunks
                            if hasattr(chunk, "web")
                        ]

        return text, grounding_chunks

    def fetch_insights(
        self,
        topic: str,
        days: int,
    ) -> ProviderResult:
        """Fetch insights based on configured sources."""
        self._log_start()

        try:
            client = self._get_client()
        except RuntimeError as e:
            return self._create_error_result(str(e))

        try:
            to_date = dt.datetime.now(dt.UTC)
            # Pad the window slightly: Google's time_range_filter rejects
            # spans of exactly 24h ("end_time must be 24 hours after
            # start_time"), which is what days=1 would otherwise produce.
            from_date = to_date - dt.timedelta(days=days, minutes=5)
            time_range = (from_date, to_date)

            sources: list[Source] = []
            seen_urls: set[str] = set()  # dedup across social + web (review L22)
            meta: dict[str, Any] = {
                "time_range": {"from": from_date.isoformat(), "to": to_date.isoformat()}
            }
            sections: list[str] = []

            # Social media query
            if "social" in self.sources:
                self._log_query("social")
                social_text, social_chunks = self._run_query(
                    client=client,
                    system_prompt=get_base_system_prompt(days),
                    user_prompt=get_user_prompt(topic, days),
                    time_range=time_range,
                )
                sources.extend(
                    self._extract_sources(social_chunks, SourceType.SOCIAL, seen_urls)
                )
                meta["social_grounding_chunks"] = social_chunks
                sections.append(f"# Social Media Analysis\n\n{social_text}")

            # Web research query
            if "web" in self.sources:
                self._log_query("web")
                web_text, web_chunks = self._run_query(
                    client=client,
                    system_prompt=get_web_system_prompt(days),
                    user_prompt=get_web_user_prompt(topic, days),
                    time_range=time_range,
                )
                sources.extend(
                    self._extract_sources(web_chunks, SourceType.WEB, seen_urls)
                )
                meta["web_grounding_chunks"] = web_chunks
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
            return self._create_error_result(f"Gemini API error: {e}")

    def generate_keywords(self, topic: str, days: int, context: str = "") -> list[str]:
        """Generate keywords using Gemini (no search grounding)."""
        try:
            from researchkit.keyword_synthesizer import parse_keyword_json
            from researchkit.prompts import (
                get_keyword_generation_system_prompt,
                get_keyword_generation_user_prompt,
            )

            client = self._get_client()
            prompt = (
                get_keyword_generation_system_prompt()
                + "\n\n---\n\n"
                + get_keyword_generation_user_prompt(topic, days, context)
            )
            response = with_network_retry(
                client.models.generate_content,
                label="gemini.generate_content:keywords",
                provider=self.provider_name,
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.5,
                ),
            )
            return parse_keyword_json(response.text or "")
        except Exception as e:
            logger.warning(f"Gemini keyword generation failed: {e}")
            return []

    def summarize_result(self, raw_text: str, topic: str) -> str:
        """
        Summarize result using Gemini.

        Args:
            raw_text: The raw text to summarize
            topic: The research topic for context

        Returns:
            Summarized text as markdown bullet points
        """
        try:
            client = self._get_client()

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

            response = with_network_retry(
                client.models.generate_content,
                label="gemini.generate_content:summarize",
                provider=self.provider_name,
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.3,
                    max_output_tokens=1000,
                ),
            )

            return response.text or ""

        except Exception as e:
            return f"*Summarization failed: {e}*\n\n{raw_text[:500]}..."
