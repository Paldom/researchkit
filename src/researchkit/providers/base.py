"""Base provider interface and data models."""

from __future__ import annotations

import logging
import math
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
    "CLI_QUICK_TIMEOUT",
    "SOCIAL_DOMAINS",
    "BaseProvider",
    "ProviderResult",
    "Source",
    "SourceType",
    "cli_harness_timeout",
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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Source:
        """Rebuild a Source from :meth:`to_dict` output (partial-result reload)."""
        try:
            source_type = SourceType(data.get("source_type") or "unknown")
        except ValueError:
            source_type = SourceType.UNKNOWN
        return cls(
            url=data["url"],
            title=data.get("title"),
            snippet=data.get("snippet"),
            date=data.get("date"),
            last_updated=data.get("last_updated"),
            source_type=source_type,
        )


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

    @property
    def failure(self) -> str | None:
        """Why this slot produced nothing (``quota``, ``timeout``, …), or None."""
        from researchkit.failures import classify_failure

        if self.error is None:
            return None if self.raw_text else "empty"
        kind = classify_failure(self.error)
        return str(kind) if kind else None

    @property
    def slot_state(self) -> str:
        """``complete`` / ``empty`` (answered, no sources) / a failure kind."""
        if self.is_success:
            return "complete" if self.sources else "empty"
        return self.failure or "error"

    def to_dict(self) -> dict[str, Any]:
        """Full serialization, including ``raw_text`` — this is the paid output.

        The bundle's own ``to_dict`` summarizes results for the report; this one
        must round-trip losslessly so an interrupted run can resume without
        re-buying what a provider already returned.
        """
        return {
            "provider": self.provider,
            "model": self.model,
            "raw_text": self.raw_text,
            "sources": [s.to_dict() for s in self.sources],
            "meta": self.meta,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProviderResult:
        """Rebuild a ProviderResult written by :meth:`to_dict`."""
        return cls(
            provider=data["provider"],
            model=data.get("model", "unknown"),
            raw_text=data.get("raw_text", ""),
            sources=[Source.from_dict(s) for s in data.get("sources", [])],
            meta=data.get("meta", {}) or {},
            error=data.get("error"),
        )


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


# Budget for one coding-agent CLI answer. A harness reasons for minutes, not
# seconds: a dense five-question prompt at the top effort tier was measured past
# 10 minutes on `codex exec`, and 300s killed that member while the other four
# answered. Losing a council member to the stopwatch costs more than waiting.
_CLI_HARNESS_TIMEOUT = 1800.0

# Budget for short-prompt pipeline steps on the same CLIs — keyword generation
# and per-provider summaries. They sit on the critical path and already degrade
# gracefully (empty keyword list, "summarization failed" stub), so a wedged one
# should cost minutes, not the full answer budget.
CLI_QUICK_TIMEOUT = 300.0

# Every headless `claude -p` researchkit spawns passes these. Hooks are the
# operator's interactive tooling, not the writer's: a repo Stop hook that runs
# the test gate turns the summary into "the hook failed" prose, and plugin
# SessionStart hooks inject personas into it. Verified on 2.1.258: --settings
# JSON overrides project/user settings; --bare would also drop OAuth, which is
# the whole point of the harness path.
CLAUDE_HEADLESS_ARGS = ("--settings", '{"disableAllHooks":true}')


def cli_harness_timeout(default: float = _CLI_HARNESS_TIMEOUT) -> float:
    """Wall-clock timeout (seconds) for one coding-agent CLI subprocess.

    Deliberately separate from :func:`provider_http_timeout`: an HTTP request
    stalled for half an hour is dead, while ``codex exec`` at ``ultra`` effort
    is still thinking. Every harness path (codex, agy, grokcli, kimicli,
    claude) takes its budget from here so one env var moves all of them.

    ``RESEARCHKIT_CLI_TIMEOUT`` (seconds) wins outright when set — it is *the*
    knob, so a stale ``PROVIDER_HTTP_TIMEOUT`` must never silently override it.
    With it unset, ``PROVIDER_HTTP_TIMEOUT`` still raises (never lowers) the
    default, keeping the previously documented escape hatch working. Values
    that aren't a positive, finite number of seconds fall back to ``default``.
    """
    raw = os.getenv("RESEARCHKIT_CLI_TIMEOUT")
    if raw:
        try:
            explicit = float(raw)
        except ValueError:
            explicit = 0.0
        if math.isfinite(explicit) and explicit > 0:
            return explicit
    return max(default, provider_http_timeout(0.0))


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

    @property
    def quota_pool(self) -> str:
        """The allowance this provider bills; CLI harnesses override it."""
        return f"{self.provider_name}-api"

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
