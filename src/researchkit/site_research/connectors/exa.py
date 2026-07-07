"""Exa connector using Exa API with deep search and Gemini summarization."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
from datetime import datetime
from typing import Any

from researchkit.network_retry import with_network_retry
from researchkit.site_research.connectors.base import BaseSiteConnector
from researchkit.site_research.types import (
    ExtractedFact,
    SiteItem,
    SiteItemSummary,
    TopicRelevance,
)

logger = logging.getLogger(__name__)

# Detailed extraction prompt for Exa search results
EXA_EXTRACTION_PROMPT = """You are analyzing web content for research on: {topic}

**Source Title:** {title}
**URL:** {url}
**Author:** {author}
**Published:** {published_at}

**CONTENT:**
{content}

**HIGHLIGHTS:**
{highlights}

---

Extract detailed, factual information from this content that is relevant to the research topic.

Return ONLY valid JSON with this exact structure:
{{
    "tldr": ["bullet 1", "bullet 2", ...],
    "key_takeaways": ["actionable insight 1", "actionable insight 2", ...],
    "extracted_facts": [
        {{"claim": "specific factual claim", "evidence": "quote or context", "confidence": "high|medium|low"}},
        ...
    ],
    "key_quotes": ["Notable quote 1", "Notable quote 2", ...],
    "statistics": ["45% of users...", "$2.3 billion market size", ...],
    "entities_mentioned": ["Company A", "Person B", "Product C", ...],
    "topic_relevance": {{
        "relevance_score": 0.0-1.0,
        "relevance_explanation": "How this content relates to {topic}",
        "topic_specific_insights": ["Insight about {topic}", ...]
    }},
    "content_type": "article|blog|news|documentation|research|forum|other"
}}

EXTRACTION GUIDELINES:
1. **tldr**: 4-6 concise bullets summarizing the main points
2. **key_takeaways**: 5-10 actionable or memorable insights
3. **extracted_facts**: 5-15 specific, verifiable claims made in the content
   - Include evidence/quotes when possible
   - Distinguish between facts and opinions
4. **key_quotes**: Direct quotes that are notable or insightful
5. **statistics**: Any numbers, percentages, data points mentioned
6. **entities_mentioned**: People, companies, products, technologies discussed
7. **topic_relevance**: How relevant is this to "{topic}"? What specifically relates?
8. **content_type**: What type of content is this?

IMPORTANT:
- Focus on information RELEVANT to "{topic}" - don't just summarize everything
- Extract SPECIFIC facts, not vague generalities
- Include evidence/context for claims when possible
- If the content is not very relevant to the topic, set relevance_score low and explain why
"""


class ExaConnector(BaseSiteConnector):
    """
    Exa connector using Exa API with deep search and Gemini summarization.

    Performs deep search with up to 100 results, fetches content and highlights,
    and summarizes using Gemini.
    """

    site_name = "exa"

    def __init__(
        self,
        api_key: str | None = None,
        gemini_api_key: str | None = None,
        gemini_model: str = "gemini-3-flash-preview",
        search_type: str = "deep",
        num_results: int = 100,
        include_context: bool = True,
        text_max_characters: int = 3000,
        highlights_per_url: int = 3,
        include_summary: bool = True,
        category: str | None = None,
        max_output_tokens: int = 8192,
        timeout_s: int = 60,
    ) -> None:
        """
        Initialize the Exa connector.

        Args:
            api_key: Exa API key (defaults to EXA_API_KEY env var)
            gemini_api_key: Gemini API key for summarization
            gemini_model: Gemini model for summarization
            search_type: Search type ("deep" or "neural")
            num_results: Maximum number of results (up to 100 for deep search)
            include_context: Whether to include RAG-friendly context string
            text_max_characters: Max characters of text per result
            highlights_per_url: Number of highlights per result
            include_summary: Whether to include Exa-generated summaries
            category: Content category filter (optional)
            max_output_tokens: Max tokens for Gemini extraction
            timeout_s: Request timeout in seconds
        """
        self.api_key = api_key or os.getenv("EXA_API_KEY")
        self.gemini_api_key = gemini_api_key or os.getenv("GEMINI_API_KEY")
        self.gemini_model = gemini_model
        self.search_type = search_type
        self.num_results = min(num_results, 100)  # Max 100 for deep search
        self.include_context = include_context
        self.text_max_characters = text_max_characters
        self.highlights_per_url = highlights_per_url
        self.include_summary = include_summary
        self.category = category
        self.max_output_tokens = max_output_tokens
        self.timeout_s = timeout_s
        self._exa_client: Any = None
        self._gemini_client: Any = None

    def is_available(self) -> bool:
        """Check if the Exa API key is configured."""
        return bool(self.api_key)

    def summarizer_is_available(self) -> bool:
        """Per-item summarize() is purely local (reads Exa's own summary/highlights
        already fetched during search) and makes no Gemini call, so it is always
        available. Gating it on GEMINI_API_KEY dropped data that was already
        present when only EXA_API_KEY was set. (Review S5.)"""
        return True

    def _get_exa_client(self) -> Any:
        """Lazy-load Exa client."""
        if self._exa_client is None:
            from exa_py import Exa

            if not self.api_key:
                raise RuntimeError("EXA_API_KEY not set")
            self._exa_client = Exa(api_key=self.api_key)
        return self._exa_client

    def _get_gemini_client(self) -> Any:
        """Lazy-load Gemini client."""
        if self._gemini_client is None:
            from google import genai

            if not self.gemini_api_key:
                raise RuntimeError("GEMINI_API_KEY not set")
            self._gemini_client = genai.Client(api_key=self.gemini_api_key)
        return self._gemini_client

    summarize_concurrency = 2  # high-volume site: keep Gemini fan-out low

    def search_batch(
        self,
        topic: str,
        keywords: list[str],
        published_after: datetime,
        limit: int,
    ) -> list[SiteItem] | None:
        """Exa runs ONCE per request: topic as query, keywords as extras."""
        return self.search(topic, published_after, self.num_results, list(keywords))

    def search(
        self,
        query: str,
        published_after: datetime,
        limit: int,
        additional_queries: list[str] | None = None,
    ) -> list[SiteItem]:
        """
        Search Exa with deep search.

        Uses query as main search, additional_queries for keyword variations.
        Returns up to num_results items with text and highlights.

        Args:
            query: Main search query (typically the research topic)
            published_after: Only return items published after this date
            limit: Maximum number of results (overridden by self.num_results)
            additional_queries: Additional query variations (keywords)

        Returns:
            List of SiteItem objects with content stored in popularity dict
        """
        if not self.api_key:
            logger.warning("Exa API key not configured, skipping Exa search")
            return []

        try:
            client = self._get_exa_client()

            # Format date for Exa API (YYYY-MM-DD)
            start_date = published_after.strftime("%Y-%m-%d")

            # Build search parameters
            search_kwargs: dict[str, Any] = {
                "query": query,
                "type": self.search_type,
                "num_results": self.num_results,
                "start_published_date": start_date,
                "text": {"max_characters": self.text_max_characters},
                "highlights": {
                    "num_sentences": self.highlights_per_url,
                    "highlights_per_url": self.highlights_per_url,
                },
            }

            # Add context if requested
            if self.include_context:
                search_kwargs["context"] = True

            # Add summary if requested
            if self.include_summary:
                search_kwargs["summary"] = True

            # Add category filter if specified
            if self.category:
                search_kwargs["category"] = self.category

            # Add additional queries (keywords) if provided
            if additional_queries:
                search_kwargs["additional_queries"] = additional_queries

            # Execute search
            response = with_network_retry(
                client.search_and_contents,
                label="exa.search_and_contents",
                provider="exa",
                **search_kwargs,
            )

            # Store context for potential RAG use
            context_str = getattr(response, "context", None)

            # Convert results to SiteItems
            results: list[SiteItem] = []
            for result in response.results:
                # Extract highlights as list
                highlights = []
                if hasattr(result, "highlights") and result.highlights:
                    highlights = list(result.highlights)

                # Extract highlight scores if available
                highlight_scores = []
                if hasattr(result, "highlight_scores") and result.highlight_scores:
                    highlight_scores = list(result.highlight_scores)

                # Get text content
                text_content = getattr(result, "text", "") or ""

                # Get Exa-generated summary if available
                exa_summary = getattr(result, "summary", None)

                results.append(
                    SiteItem(
                        site="exa",
                        query=query,
                        title=getattr(result, "title", "") or "",
                        url=getattr(result, "url", "") or "",
                        author_or_channel=getattr(result, "author", None),
                        published_at=getattr(result, "published_date", None),
                        popularity={
                            # Store content in popularity dict for summarization
                            "text": text_content,
                            "highlights": highlights,
                            "highlight_scores": highlight_scores,
                            "exa_summary": exa_summary,
                            "exa_id": getattr(result, "id", None),
                            # Store context at bundle level (first item only)
                            "context": context_str if not results else None,
                        },
                        summary=None,
                    )
                )

            logger.info(
                f"Exa search returned {len(results)} results for query: {query[:50]}"
            )
            return results

        except Exception as e:
            # Re-raise so the orchestrator records this in bundle.errors rather
            # than silently returning empty (indistinguishable from "no results").
            # (Review M12.)
            logger.warning(f"Exa search failed for query '{query}': {e}")
            raise

    def summarize(self, topic: str, item: SiteItem) -> SiteItemSummary:
        """
        Return Exa's built-in summary for individual items.

        We skip per-item Gemini calls to avoid rate limiting.
        Use summarize_batch() for a single Gemini call over all items.

        Args:
            topic: The research topic for context
            item: The SiteItem to summarize

        Returns:
            SiteItemSummary using Exa's built-in summary
        """
        exa_summary = item.popularity.get("exa_summary")
        highlights = item.popularity.get("highlights", [])

        # Build tldr from exa_summary and highlights
        tldr: list[str] = []
        if exa_summary:
            tldr.append(exa_summary)
        if highlights:
            tldr.extend(highlights[:3])

        if not tldr:
            tldr = ["No summary available"]

        return SiteItemSummary(
            tldr=tldr,
            key_takeaways=highlights if highlights else [],
        )

    def summarize_batch(self, topic: str, items: list[SiteItem]) -> str:
        """
        Summarize all Exa items in a single Gemini call.

        Args:
            topic: The research topic
            items: List of SiteItems to summarize

        Returns:
            Markdown summary of all items
        """
        from google.genai import types

        if not self.summarizer_is_available():
            return "*Batch summarization unavailable - GEMINI_API_KEY not set*"

        if not items:
            return "*No Exa results to summarize*"

        # Build context from all items
        item_summaries = []
        for i, item in enumerate(items[:50], 1):  # Limit to 50 for context size
            exa_summary = item.popularity.get("exa_summary", "")
            highlights = item.popularity.get("highlights", [])
            highlights_str = " | ".join(highlights[:2]) if highlights else ""

            item_summaries.append(
                f"{i}. **{item.title}**\n"
                f"   URL: {item.url}\n"
                f"   Summary: {exa_summary}\n"
                f"   Highlights: {highlights_str}"
            )

        items_text = "\n\n".join(item_summaries)

        prompt = f'''You are synthesizing web research results on: **{topic}**

Here are {len(items)} search results from Exa deep search:

{items_text}

---

Provide a comprehensive synthesis of these sources. Return markdown with:

## Key Themes
- Major themes and patterns across sources

## Notable Findings
- Specific insights, facts, or claims that appear across multiple sources
- Include source numbers [1], [2], etc. when citing

## Diverse Perspectives
- Different viewpoints or approaches mentioned

## Actionable Insights
- Practical takeaways for someone researching {topic}

Be specific and cite source numbers. Focus on information relevant to "{topic}".
'''

        try:
            client = self._get_gemini_client()
            response = with_network_retry(
                client.models.generate_content,
                label="exa.gemini.generate_content:batch_summary",
                provider="exa",
                model=self.gemini_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    max_output_tokens=4096,
                ),
            )
            return response.text or "*No response from summarizer*"

        except Exception as e:
            logger.warning(f"Exa batch summarization failed: {e}")
            return f"*Batch summarization failed: {str(e)[:100]}*"

    def _parse_extraction_response(self, text: str) -> SiteItemSummary:
        """Parse JSON extraction response into SiteItemSummary."""
        text = text.strip()

        # Extract JSON from response
        data: dict[str, Any] = {}
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{[\s\S]*\}", text)
            if match:
                with contextlib.suppress(json.JSONDecodeError):
                    data = json.loads(match.group(0))

        if not data:
            # Fallback: use raw text as tldr
            return SiteItemSummary(
                tldr=[text[:500] if text else "Failed to parse response"],
                summarization_error="JSON parse failed",
            )

        # Parse extracted_facts
        extracted_facts = []
        for fact_data in data.get("extracted_facts", []):
            if isinstance(fact_data, dict):
                extracted_facts.append(
                    ExtractedFact(
                        claim=str(fact_data.get("claim", "")),
                        evidence=fact_data.get("evidence"),
                        confidence=fact_data.get("confidence", "medium"),
                    )
                )

        # Parse topic_relevance
        topic_relevance = None
        if tr_data := data.get("topic_relevance"):
            topic_relevance = TopicRelevance(
                relevance_score=float(tr_data.get("relevance_score", 0.5)),
                relevance_explanation=str(tr_data.get("relevance_explanation", "")),
                topic_specific_insights=_ensure_list(
                    tr_data.get("topic_specific_insights", [])
                ),
            )

        return SiteItemSummary(
            tldr=_ensure_list(data.get("tldr", [])),
            key_takeaways=_ensure_list(data.get("key_takeaways", [])),
            extracted_facts=extracted_facts,
            key_quotes=_ensure_list(data.get("key_quotes", [])),
            statistics=_ensure_list(data.get("statistics", [])),
            entities_mentioned=_ensure_list(data.get("entities_mentioned", [])),
            topic_relevance=topic_relevance,
            content_type=data.get("content_type"),
        )

    def popularity_score(self, item: SiteItem) -> float:
        """
        Return a popularity score for ranking.

        Exa doesn't provide view counts, so we use highlight scores if available,
        otherwise return 0 (items are already ranked by Exa's relevance).
        """
        highlight_scores = item.popularity.get("highlight_scores", [])
        if highlight_scores:
            return sum(float(s) for s in highlight_scores) / len(highlight_scores)
        return 0.0


def _ensure_list(val: Any) -> list[str]:
    """Ensure value is a list of strings."""
    if not isinstance(val, list):
        return [str(val)] if val else []
    return [str(x).strip() for x in val if x and str(x).strip()]
