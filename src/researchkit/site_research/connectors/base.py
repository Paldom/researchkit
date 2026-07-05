"""Base class for site connectors."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from researchkit.site_research.types import SiteItem, SiteItemSummary


class BaseSiteConnector(ABC):
    """Abstract base class for site connectors."""

    site_name: str = "base"

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
