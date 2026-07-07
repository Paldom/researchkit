"""Site researcher orchestrator."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from researchkit.site_research.connectors.base import BaseSiteConnector
from researchkit.site_research.types import (
    SiteItem,
    SiteItemSummary,
    SiteResearchBundle,
    SiteResearchConfig,
)

logger = logging.getLogger(__name__)


class SiteResearcher:
    """
    Orchestrates site research across multiple connectors.

    Handles:
    - Parallel keyword search across sites
    - Deduplication by URL
    - Popularity-based ranking
    - Parallel summarization (via connector.summarize())
    """

    def __init__(
        self,
        connectors: dict[str, BaseSiteConnector],
    ) -> None:
        """
        Initialize the site researcher.

        Args:
            connectors: Dict mapping site names to connector instances
                        (each connector handles its own summarization)
        """
        self.connectors = connectors

    async def run(
        self,
        topic: str,
        keywords: list[str],
        days: int,
        config: SiteResearchConfig,
    ) -> SiteResearchBundle:
        """
        Run site research across all configured sites.

        Args:
            topic: The research topic
            keywords: List of search keywords
            days: Lookback window in days
            config: Site research configuration

        Returns:
            SiteResearchBundle with results
        """
        logger.info(
            f"Starting site research: {len(keywords)} keywords, "
            f"sites={config.sites}, days={days}"
        )

        published_after = datetime.now(UTC) - timedelta(days=days)
        used_keywords = [k.strip() for k in keywords if k.strip()][
            : config.max_keywords_used
        ]

        bundle = SiteResearchBundle(
            config=config.to_dict(),
            items_by_site={},
            errors=[],
        )

        if not used_keywords:
            bundle.errors.append("No keywords provided for site research")
            return bundle

        # Phase 1: Search all sites x keywords
        # Sites that require sequential requests due to rate limiting
        # Sites whose APIs rate-limit hard enough to need sequential queries
        # (declared by the connector itself).
        sequential_sites: set[str] = {
            site for site, c in self.connectors.items() if c.sequential
        }

        async def _search(connector: BaseSiteConnector, query: str) -> list[SiteItem]:
            return await asyncio.to_thread(
                connector.search,
                query,
                published_after,
                config.per_keyword_max_results,
            )

        for site in config.sites:
            if site not in self.connectors:
                bundle.errors.append(f"Site connector not found: {site}")
                continue

            connector = self.connectors[site]
            if not connector.is_available():
                bundle.errors.append(
                    f"Site connector not available (missing API key): {site}"
                )
                continue

            all_items: list[SiteItem] = []

            # Exclusive batch hook: a connector may serve the whole request
            # in one call (exa does); non-None replaces per-keyword search.
            batch_items: list[SiteItem] | None = None
            try:
                batch_items = await asyncio.to_thread(
                    connector.search_batch,
                    topic,
                    used_keywords,
                    published_after,
                    config.get_max_items(site),
                )
            except Exception as e:
                logger.warning(f"{site} batch search error: {e}")
                bundle.errors.append(f"{site} search error: {str(e)[:100]}")

            if batch_items is not None:
                all_items.extend(batch_items)
            elif site in sequential_sites:
                # Run searches sequentially for rate-limited APIs
                for query in used_keywords:
                    try:
                        items = await _search(connector, query)
                        all_items.extend(items)
                        # Small delay between requests to avoid rate limiting
                        await asyncio.sleep(0.5)
                    except Exception as e:
                        logger.warning(f"Search error for {site} query '{query}': {e}")
                        bundle.errors.append(f"{site} search error: {str(e)[:100]}")
            else:
                # Run searches in parallel for sites that support it
                search_semaphore = asyncio.Semaphore(4)

                async def _search_with_semaphore(
                    query: str,
                    _sem: asyncio.Semaphore = search_semaphore,
                    _connector: BaseSiteConnector = connector,
                ) -> list[SiteItem]:
                    async with _sem:
                        return await _search(_connector, query)

                tasks = [
                    asyncio.create_task(_search_with_semaphore(query))
                    for query in used_keywords
                ]

                for task in asyncio.as_completed(tasks):
                    try:
                        items = await task
                        all_items.extend(items)
                    except Exception as e:
                        logger.warning(f"Search error for {site}: {e}")
                        bundle.errors.append(f"{site} search error: {str(e)[:100]}")

            # Connector-rendered popularity for report display (kept on the
            # item so formatting stays connector-agnostic).
            for item in all_items:
                if not item.popularity_display:
                    # display-only; never fatal
                    with contextlib.suppress(Exception):
                        item.popularity_display = connector.popularity_label(item)

            # Dedupe by URL
            seen_urls: set[str] = set()
            unique_items: list[SiteItem] = []
            for item in all_items:
                if item.url and item.url not in seen_urls:
                    seen_urls.add(item.url)
                    unique_items.append(item)

            # Sort by popularity
            unique_items.sort(
                key=lambda x: connector.popularity_score(x),
                reverse=True,
            )

            # Take top N (per-site limit)
            max_items = config.get_max_items(site)
            bundle.items_by_site[site] = unique_items[:max_items]

            logger.info(
                f"Site {site}: found {len(all_items)} items, "
                f"unique={len(unique_items)}, selected={len(bundle.items_by_site[site])} (max={max_items})"
            )

        # Phase 2: Summarize selected items (using connector.summarize())
        await self._summarize_items(topic, bundle)

        # Phase 3: Batch summarization for Exa (single Gemini call)
        await self._batch_summarize(topic, bundle)

        # Phase 4: Generate digest markdown
        bundle.digest_markdown = self._generate_digest(bundle)

        logger.info(
            f"Site research complete: {bundle.total_items()} items, "
            f"{len(bundle.errors)} errors"
        )

        return bundle

    async def _summarize_items(
        self,
        topic: str,
        bundle: SiteResearchBundle,
    ) -> None:
        """Summarize all items using each connector's summarize method."""

        async def _summarize(
            site: str, item: SiteItem, semaphore: asyncio.Semaphore
        ) -> None:
            # Mutates item.summary in place. Handles its own errors so a single
            # failed summarization keeps the item (with an error summary) rather
            # than dropping it from the list. (Review M11.)
            async with semaphore:
                connector = self.connectors.get(site)
                if connector and connector.summarizer_is_available():
                    try:
                        item.summary = await asyncio.to_thread(
                            connector.summarize, topic, item
                        )
                    except Exception as e:
                        logger.warning(f"Summarization error for {site}: {e}")
                        bundle.errors.append(
                            f"{site} summarization error: {str(e)[:100]}"
                        )
                        item.summary = SiteItemSummary(
                            tldr=["Summarization failed"],
                            summarization_error=str(e)[:200],
                        )
                    # Small delay to avoid rate limiting bursts
                    await asyncio.sleep(0.2)
                else:
                    item.summary = SiteItemSummary(
                        tldr=["Summarization unavailable"],
                        summarization_error=f"Summarizer not available for {site}",
                    )

        for site, items in bundle.items_by_site.items():
            connector = self.connectors.get(site)
            concurrency = connector.summarize_concurrency if connector else 3
            summarize_semaphore = asyncio.Semaphore(concurrency)

            tasks = [
                asyncio.create_task(_summarize(site, item, summarize_semaphore))
                for item in items
            ]

            # Run for side effects only; items are mutated in place, so the
            # original popularity-sorted order (Phase 1) is preserved instead of
            # being scrambled into task-completion order. (Review M11.)
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _batch_summarize(
        self,
        topic: str,
        bundle: SiteResearchBundle,
    ) -> None:
        """Additive batch summaries for connectors that provide them."""
        for site, items in bundle.items_by_site.items():
            connector = self.connectors.get(site)
            if not connector or not items:
                continue
            try:
                batch_summary = await asyncio.to_thread(
                    connector.summarize_batch, topic, items
                )
            except Exception as e:
                logger.warning(f"{site} batch summarization failed: {e}")
                bundle.errors.append(
                    f"{site} batch summarization error: {str(e)[:100]}"
                )
                continue
            if batch_summary:
                bundle.batch_summaries[site] = batch_summary
                logger.info(f"{site} batch summarization complete")

    def _generate_digest(self, bundle: SiteResearchBundle) -> str:
        """Generate an enhanced markdown digest with detailed summaries."""
        lines = ["## Site Research Digest", ""]

        for site, items in bundle.items_by_site.items():
            if not items:
                continue

            lines.append(f"### {site.title()}")
            lines.append("")

            # Include batch summary if available (e.g., for Exa)
            if bundle.batch_summaries.get(site):
                lines.append("#### Synthesis")
                lines.append(bundle.batch_summaries[site])
                lines.append("")
                lines.append("#### Sources")
                lines.append("")

            for item in items:
                # Title and URL
                lines.append(f"**[{item.title}]({item.url})**")

                if item.summary:
                    # TL;DR bullets
                    if item.summary.tldr:
                        for bullet in item.summary.tldr[:2]:  # Shorter for Exa
                            lines.append(f"- {bullet}")

                    # Topic-specific insights (only for non-batch sites)
                    if (
                        site not in bundle.batch_summaries
                        and item.summary.topic_relevance
                    ):
                        tr = item.summary.topic_relevance
                        if tr.topic_specific_insights:
                            score_str = f" (relevance: {tr.relevance_score:.1f})"
                            lines.append(f"**Topic Insights{score_str}:**")
                            for insight in tr.topic_specific_insights[:3]:
                                lines.append(f"- {insight}")

                    # Key extracted facts (only for non-batch sites)
                    if (
                        site not in bundle.batch_summaries
                        and item.summary.extracted_facts
                    ):
                        lines.append("**Key Facts:**")
                        for fact in item.summary.extracted_facts[:4]:
                            lines.append(f"- {fact.claim}")

                    # Statistics (only for non-batch sites)
                    if site not in bundle.batch_summaries and item.summary.statistics:
                        lines.append("**Statistics:**")
                        for stat in item.summary.statistics[:3]:
                            lines.append(f"- {stat}")

                lines.append("")

        if not bundle.items_by_site or all(
            not items for items in bundle.items_by_site.values()
        ):
            lines.append("*No site research results found.*")
            lines.append("")

        return "\n".join(lines)


def create_site_researcher(
    sites: list[str] | None = None,
    summarizer_model: str = "gemini-3-flash-preview",
    exa_config: dict[str, Any] | None = None,
    plugin_options: dict[str, dict[str, Any]] | None = None,
) -> SiteResearcher:
    """
    Factory building a SiteResearcher from the plugin registry.

    Every connector — built-in or plugin — is constructed through its
    registered spec with a uniform ConnectorContext. ``exa_config`` is kept
    as a legacy alias for exa's options; per-connector options otherwise
    come from the preset's ``plugins:`` block via ``plugin_options``.

    Args:
        sites: Sites to enable (default: every registered connector)
        summarizer_model: Model for connector summarization
        exa_config: Legacy exa options (merged into exa's options dict)
        plugin_options: Per-connector options keyed by connector name

    Returns:
        Configured SiteResearcher instance
    """
    from researchkit.plugin_api import ConnectorContext
    from researchkit.plugins import get_registry

    registry = get_registry()
    options_by_name = dict(plugin_options or {})
    if exa_config:
        options_by_name["exa"] = {**exa_config, **options_by_name.get("exa", {})}

    wanted = sites if sites else list(registry.connectors)
    connectors: dict[str, BaseSiteConnector] = {}
    for name in wanted:
        spec = registry.connectors.get(name)
        if spec is None:
            continue  # unknown sites surface later as bundle errors
        ctx = ConnectorContext(
            summarizer_model=summarizer_model,
            options=options_by_name.get(name, {}),
        )
        try:
            connectors[name] = spec.factory(ctx)
        except Exception as e:  # one bad connector never sinks the rest
            logger.warning("Connector %s failed to construct: %s", name, e)

    return SiteResearcher(connectors=connectors)
