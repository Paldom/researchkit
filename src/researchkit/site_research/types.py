"""Data types for site research module."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SiteResearchConfig:
    """Configuration for site research."""

    enabled: bool = True
    sites: list[str] = field(default_factory=lambda: ["exa"])
    max_keywords_used: int = 5
    per_keyword_max_results: int = 10

    # Per-site max items limits
    max_items_youtube: int = 15
    max_items_medium: int = 50
    max_items_exa: int = 100

    # Exa-specific settings
    exa_search_type: str = "deep"
    exa_num_results: int = 100
    exa_context: bool = True
    exa_text_max_characters: int = 3000
    exa_highlights_per_url: int = 3
    exa_include_summary: bool = True
    exa_category: str | None = None

    def get_max_items(self, site: str) -> int:
        """Get the max items limit for a specific site."""
        limits = {
            "youtube": self.max_items_youtube,
            "medium": self.max_items_medium,
            "exa": self.max_items_exa,
        }
        return limits.get(site, 15)  # Default to 15 for unknown sites

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        result = {
            "enabled": self.enabled,
            "sites": self.sites,
            "max_keywords_used": self.max_keywords_used,
            "per_keyword_max_results": self.per_keyword_max_results,
            "max_items_youtube": self.max_items_youtube,
            "max_items_medium": self.max_items_medium,
            "max_items_exa": self.max_items_exa,
            "exa_search_type": self.exa_search_type,
            "exa_num_results": self.exa_num_results,
            "exa_context": self.exa_context,
            "exa_text_max_characters": self.exa_text_max_characters,
            "exa_highlights_per_url": self.exa_highlights_per_url,
            "exa_include_summary": self.exa_include_summary,
        }
        if self.exa_category:
            result["exa_category"] = self.exa_category
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SiteResearchConfig:
        """Create from dictionary."""
        return cls(
            enabled=data.get("enabled", True),
            sites=data.get("sites", ["exa"]),
            max_keywords_used=data.get("max_keywords_used", 5),
            per_keyword_max_results=data.get("per_keyword_max_results", 10),
            max_items_youtube=data.get("max_items_youtube", 15),
            max_items_medium=data.get("max_items_medium", 50),
            max_items_exa=data.get("max_items_exa", 100),
            exa_search_type=data.get("exa_search_type", "deep"),
            exa_num_results=data.get("exa_num_results", 100),
            exa_context=data.get("exa_context", True),
            exa_text_max_characters=data.get("exa_text_max_characters", 3000),
            exa_highlights_per_url=data.get("exa_highlights_per_url", 3),
            exa_include_summary=data.get("exa_include_summary", True),
            exa_category=data.get("exa_category"),
        )


@dataclass
class ExtractedFact:
    """A specific fact extracted from the content."""

    claim: str
    evidence: str | None = None
    confidence: str = "medium"  # high/medium/low

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "claim": self.claim,
            "evidence": self.evidence,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExtractedFact:
        """Create from dictionary."""
        return cls(
            claim=data.get("claim", ""),
            evidence=data.get("evidence"),
            confidence=data.get("confidence", "medium"),
        )


@dataclass
class TopicRelevance:
    """How the content relates to the research topic."""

    relevance_score: float  # 0.0 to 1.0
    relevance_explanation: str
    topic_specific_insights: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "relevance_score": self.relevance_score,
            "relevance_explanation": self.relevance_explanation,
            "topic_specific_insights": self.topic_specific_insights,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TopicRelevance:
        """Create from dictionary."""
        return cls(
            relevance_score=data.get("relevance_score", 0.5),
            relevance_explanation=data.get("relevance_explanation", ""),
            topic_specific_insights=data.get("topic_specific_insights", []),
        )


@dataclass
class SiteItemSummary:
    """Enhanced summary of a site research item."""

    # Core summary
    tldr: list[str] = field(default_factory=list)  # 4-6 bullets
    key_takeaways: list[str] = field(default_factory=list)  # 5-10 insights

    # Detailed extraction
    extracted_facts: list[ExtractedFact] = field(default_factory=list)  # 5-15 facts
    key_quotes: list[str] = field(default_factory=list)  # Notable quotes
    statistics: list[str] = field(default_factory=list)  # Numbers, data points
    entities_mentioned: list[str] = field(default_factory=list)  # People, companies

    # Topic relevance
    topic_relevance: TopicRelevance | None = None

    # Content metadata
    content_type: str | None = None  # tutorial/opinion/news/review/etc.

    # Error handling
    summarization_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        result: dict[str, Any] = {
            "tldr": self.tldr,
            "key_takeaways": self.key_takeaways,
            "extracted_facts": [f.to_dict() for f in self.extracted_facts],
            "key_quotes": self.key_quotes,
            "statistics": self.statistics,
            "entities_mentioned": self.entities_mentioned,
        }
        if self.topic_relevance:
            result["topic_relevance"] = self.topic_relevance.to_dict()
        if self.content_type:
            result["content_type"] = self.content_type
        if self.summarization_error:
            result["summarization_error"] = self.summarization_error
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SiteItemSummary:
        """Create from dictionary."""
        # Parse extracted_facts
        extracted_facts = []
        for fact_data in data.get("extracted_facts", []):
            if isinstance(fact_data, dict):
                extracted_facts.append(ExtractedFact.from_dict(fact_data))

        # Parse topic_relevance
        topic_relevance = None
        if tr_data := data.get("topic_relevance"):
            topic_relevance = TopicRelevance.from_dict(tr_data)

        return cls(
            tldr=data.get("tldr", []),
            key_takeaways=data.get("key_takeaways", []),
            extracted_facts=extracted_facts,
            key_quotes=data.get("key_quotes", []),
            statistics=data.get("statistics", []),
            entities_mentioned=data.get("entities_mentioned", []),
            topic_relevance=topic_relevance,
            content_type=data.get("content_type"),
            summarization_error=data.get("summarization_error"),
        )

    def to_markdown(self) -> str:
        """Render the extraction as readable markdown (materials fallback)."""
        parts: list[str] = []
        if self.tldr:
            parts.append("## TL;DR\n" + "\n".join(f"- {b}" for b in self.tldr))
        if self.key_takeaways:
            parts.append(
                "## Key takeaways\n" + "\n".join(f"- {b}" for b in self.key_takeaways)
            )
        if self.extracted_facts:
            facts = "\n".join(
                f"- {f.claim}" + (f" (evidence: {f.evidence})" if f.evidence else "")
                for f in self.extracted_facts
            )
            parts.append("## Extracted facts\n" + facts)
        if self.key_quotes:
            parts.append("## Quotes\n" + "\n".join(f"> {q}" for q in self.key_quotes))
        if self.statistics:
            parts.append(
                "## Statistics\n" + "\n".join(f"- {s}" for s in self.statistics)
            )
        return "\n\n".join(parts).strip()


@dataclass
class SiteItem:
    """A single item from site research (YouTube video or Medium article)."""

    site: str
    query: str
    title: str
    url: str
    author_or_channel: str | None = None
    published_at: str | None = None
    popularity: dict[str, Any] = field(default_factory=dict)
    summary: SiteItemSummary | None = None
    # Canonical platform text captured by the connector (YouTube transcript,
    # Medium article body, or a rendered summary) so the materials archive
    # never has to re-query the platform. See EXTRAS.md.
    content: str = ""
    content_kind: str = ""  # transcript | article | summary
    # Connector-rendered popularity string ("Views: 1.2M") so report
    # formatting never needs to know connector names.
    popularity_display: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        result: dict[str, Any] = {
            "site": self.site,
            "query": self.query,
            "title": self.title,
            "url": self.url,
            "popularity": self.popularity,
        }
        if self.author_or_channel:
            result["author_or_channel"] = self.author_or_channel
        if self.published_at:
            result["published_at"] = self.published_at
        if self.summary:
            result["summary"] = self.summary.to_dict()
        if self.content:
            result["content"] = self.content
            result["content_kind"] = self.content_kind or "article"
        if self.popularity_display:
            result["popularity_display"] = self.popularity_display
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SiteItem:
        """Create from dictionary."""
        summary_data = data.get("summary")
        return cls(
            site=data["site"],
            query=data["query"],
            title=data["title"],
            url=data["url"],
            author_or_channel=data.get("author_or_channel"),
            published_at=data.get("published_at"),
            popularity=data.get("popularity", {}),
            content=str(data.get("content", "")),
            content_kind=str(data.get("content_kind", "")),
            popularity_display=str(data.get("popularity_display", "")),
            summary=SiteItemSummary.from_dict(summary_data) if summary_data else None,
        )


@dataclass
class SiteResearchBundle:
    """Bundle of site research results."""

    config: dict[str, Any] = field(default_factory=dict)
    items_by_site: dict[str, list[SiteItem]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    digest_markdown: str | None = None
    batch_summaries: dict[str, str] = field(
        default_factory=dict
    )  # Per-site batch summaries

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "config": self.config,
            "items_by_site": {
                site: [item.to_dict() for item in items]
                for site, items in self.items_by_site.items()
            },
            "errors": self.errors,
            "digest_markdown": self.digest_markdown,
            "batch_summaries": self.batch_summaries,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SiteResearchBundle:
        """Create from dictionary."""
        items_by_site = {}
        for site, items in data.get("items_by_site", {}).items():
            items_by_site[site] = [SiteItem.from_dict(item) for item in items]

        return cls(
            config=data.get("config", {}),
            items_by_site=items_by_site,
            errors=data.get("errors", []),
            digest_markdown=data.get("digest_markdown"),
            batch_summaries=data.get("batch_summaries", {}),
        )

    def total_items(self) -> int:
        """Get total number of items across all sites."""
        return sum(len(items) for items in self.items_by_site.values())
