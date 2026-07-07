"""Base class for site connectors."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from researchkit.site_research.types import SiteItem, SiteItemSummary


class BaseSiteConnector(ABC):
    """Abstract base class for site connectors.

    Optional hooks (all have safe defaults) let a connector customize
    orchestration without the orchestrator knowing its name:

    - ``search_batch`` is EXCLUSIVE: a non-None return replaces the
      per-keyword ``search`` loop entirely (one call for the whole request).
    - ``summarize_batch`` is ADDITIVE: it runs after per-item summaries and
      contributes an extra digest section; per-item summaries are kept.
    - ``popularity_label`` renders a display string ("Views: 1.2M") stored
      on the item so report formatting stays connector-agnostic.
    - ``sequential`` serializes this connector's keyword queries (hard rate
      limits); ``summarize_concurrency`` caps its summarization fan-out.
    """

    site_name: str = "base"
    sequential: bool = False
    summarize_concurrency: int = 3

    def search_batch(
        self,
        topic: str,
        keywords: list[str],
        published_after: datetime,
        limit: int,
    ) -> list[SiteItem] | None:
        """One-shot search for the whole request; None = use per-keyword search."""
        return None

    def summarize_batch(self, topic: str, items: list[SiteItem]) -> str | None:
        """Cross-item digest section; None = no batch summary."""
        return None

    def popularity_label(self, item: SiteItem) -> str:
        """Human-readable popularity string for report display ("" = none)."""
        return ""

    @abstractmethod
    def search(
        self,
        query: str,
        published_after: datetime,
        limit: int,
    ) -> list[SiteItem]:
        """
        Search for items matching the query.

        Args:
            query: Search query string
            published_after: Only return items published after this date
            limit: Maximum number of results to return

        Returns:
            List of SiteItem objects (without summaries)
        """
        pass

    @abstractmethod
    def summarize(
        self,
        topic: str,
        item: SiteItem,
    ) -> SiteItemSummary:
        """
        Generate a detailed, topic-relevant summary of the item.

        Args:
            topic: The research topic (for relevance extraction)
            item: The SiteItem to summarize

        Returns:
            SiteItemSummary with detailed extraction
        """
        pass

    @abstractmethod
    def popularity_score(self, item: SiteItem) -> float:
        """
        Calculate a popularity score for ranking.

        Args:
            item: The SiteItem to score

        Returns:
            A float score for ranking (higher = more popular)
        """
        pass

    def is_available(self) -> bool:
        """
        Check if this connector is available (has required API keys for search).

        Returns:
            True if the connector can be used for searching, False otherwise
        """
        return True

    def summarizer_is_available(self) -> bool:
        """
        Check if summarization is available (may require different API key).

        Returns:
            True if summarization can be used, False otherwise
        """
        return True
