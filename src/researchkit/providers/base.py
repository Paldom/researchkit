"""Base provider interface and data models."""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from researchkit.prompts import (
    get_social_system_prompt as get_base_system_prompt,
)
from researchkit.prompts import get_social_user_prompt as get_user_prompt
from researchkit.prompts import (
    get_web_system_prompt,
    get_web_user_prompt,
)

# Explicit public surface (mypy strict: re-exported prompt helpers included).
__all__ = [
    "SOCIAL_DOMAINS",
    "BaseProvider",
    "ProviderResult",
    "Source",
    "SourceType",
    "get_base_system_prompt",
    "get_user_prompt",
    "get_web_system_prompt",
    "get_web_user_prompt",
    "provider_http_timeout",
    "recency_to_glm_filter",
    "recency_to_perplexity_filter",
]

logger = logging.getLogger(__name__)


class SourceType(Enum):
    """Type of source for categorization."""

    SOCIAL = "social"
    WEB = "web"
    UNKNOWN = "unknown"


@dataclass
class Source:
    """
    Normalized source/citation from any provider.

    This provides a standard interface for citations regardless of whether
    they come from Gemini grounding chunks, OpenAI annotations, Perplexity
    search results, or Grok citations.
    """

    url: str
    title: str | None = None
    snippet: str | None = None
    date: str | None = None
    last_updated: str | None = None
    source_type: SourceType = SourceType.UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        result = {
            "url": self.url,
            "title": self.title,
            "source_type": self.source_type.value,
        }
        if self.snippet:
            result["snippet"] = self.snippet
        if self.date:
            result["date"] = self.date
        if self.last_updated:
            result["last_updated"] = self.last_updated
        return result


@dataclass
class ProviderResult:
    """Result from a provider's insight collection."""

    provider: str
    model: str
    raw_text: str
    sources: list[Source] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def is_success(self) -> bool:
        """Check if the result was successful."""
        return self.error is None and bool(self.raw_text)


# Domain lists for filtering searches to social/discussion sources
SOCIAL_DOMAINS = [
    # Major social platforms
    "reddit.com",
    "old.reddit.com",
    "x.com",
    "twitter.com",
    "tiktok.com",
    "instagram.com",
    "facebook.com",
    "threads.net",
    "youtube.com",
    "youtu.be",
    "linkedin.com",
    # Tech & discussion communities
    "news.ycombinator.com",
    "dev.to",
    "hashnode.com",
    "stackexchange.com",
    "stackoverflow.com",
    "quora.com",
    # Blogging platforms
    "medium.com",
    "substack.com",
    "wordpress.com",
    "blogspot.com",
    "ghost.io",
    # Dev / project chatter
    "github.com",
    "gitlab.com",
    "discord.com",
    "mastodon.social",
]


def provider_http_timeout(default: float = 180.0) -> float:
    """HTTP timeout (seconds) for provider API clients.

    Overridable via the ``PROVIDER_HTTP_TIMEOUT`` env var. Web-search-heavy
    calls (OpenAI web_search + reasoning, GLM web_search synthesis with large
    ``count``) routinely exceed 60s, so the default is 180s to avoid spurious
    timeouts on slow searches. Measured worst cases: OpenAI ~112s/query,
    GLM ~76s for social+web — 180s leaves comfortable headroom.
    """
    try:
        return float(os.getenv("PROVIDER_HTTP_TIMEOUT", default))
    except (TypeError, ValueError):
        return default


def recency_to_perplexity_filter(days: int) -> str:
    """Convert days to Perplexity recency filter value."""
    if days <= 1:
        return "day"
    elif days <= 7:
        return "week"
    elif days <= 31:
        return "month"
    return "year"


def recency_to_glm_filter(days: int) -> str:
    """Convert a lookback window in days to a z.ai GLM web-search recency filter.

    Rounds *up* to the nearest supported bucket so the search window always
    covers the requested range (e.g. 5 days -> ``"oneWeek"``). Accepted values
    are ``oneDay``, ``oneWeek``, ``oneMonth``, ``oneYear`` and ``noLimit``.
    """
    if days <= 1:
        return "oneDay"
    elif days <= 7:
        return "oneWeek"
    elif days <= 30:
        return "oneMonth"
    elif days <= 365:
        return "oneYear"
    return "noLimit"


class BaseProvider(ABC):
    """Abstract base class for insight providers."""

    provider_name: str = "base"
    model_name: str = "unknown"

    @abstractmethod
    def fetch_insights(
        self,
        topic: str,
        days: int,
    ) -> ProviderResult:
        """
        Fetch social insights for a given topic.

        Args:
            topic: The topic to research
            days: Number of days to look back

        Returns:
            ProviderResult with the collected insights
        """
        pass

    def _create_error_result(self, error: str) -> ProviderResult:
        """Create an error result."""
        logger.error(
            f"Provider error: {error}",
            extra={"stage": "provider_error", "provider": self.provider_name},
        )
        return ProviderResult(
            provider=self.provider_name,
            model=self.model_name,
            raw_text="",
            error=error,
        )

    def _log_start(self) -> None:
        """Log provider fetch start."""
        logger.info(
            "Provider fetch started",
            extra={"stage": "provider_fetch_start", "provider": self.provider_name},
        )

    def _log_query(self, query_type: str) -> None:
        """Log a query being run."""
        logger.debug(
            f"Running {query_type} query",
            extra={"stage": "provider_query", "provider": self.provider_name},
        )

    def _log_done(self, sources_count: int, chars: int) -> None:
        """Log provider fetch completion."""
        logger.info(
            f"Provider fetch done: {sources_count} sources, {chars} chars",
            extra={"stage": "provider_fetch_done", "provider": self.provider_name},
        )

    def generate_keywords(self, topic: str, days: int, context: str = "") -> list[str]:
        """
        Generate search keywords for a topic using this provider's LLM.

        Each LLM provider implements this to generate keywords using its own
        API, enabling multi-provider keyword generation with diverse
        perspectives.

        Args:
            topic: The research topic
            days: Lookback window in days
            context: Optional grounding context from research findings

        Returns:
            List of keyword phrases (target: 10 items)
        """
        logger.warning(
            f"generate_keywords not implemented for {self.provider_name}, returning empty",
            extra={"stage": "keyword_gen_fallback", "provider": self.provider_name},
        )
        return []

    def summarize_result(self, raw_text: str, topic: str) -> str:
        """
        Summarize this provider's raw result using its own model.

        Each provider implements this to summarize results using its own API/model,
        enabling self-summarization (e.g., Grok results summarized by Grok).

        Args:
            raw_text: The raw text from the provider's fetch_insights result
            topic: The research topic (for context)

        Returns:
            Summarized text as markdown bullet points (5-8 points)
        """
        # Default implementation - subclasses should override
        logger.warning(
            f"summarize_result not implemented for {self.provider_name}, using fallback",
            extra={"stage": "summarize_fallback", "provider": self.provider_name},
        )
        return raw_text[:500] + "..." if len(raw_text) > 500 else raw_text
