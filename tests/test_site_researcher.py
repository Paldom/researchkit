"""SiteResearcher orchestration: sites run concurrently, keyword-free first."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

from researchkit.site_research import (
    SiteItem,
    SiteItemSummary,
    SiteResearchConfig,
    SiteResearcher,
)
from researchkit.site_research.connectors.base import BaseSiteConnector


class SlowConnector(BaseSiteConnector):
    """Blocks for ``delay`` seconds per search and records when it ran."""

    def __init__(self, name: str, delay: float = 0.0) -> None:
        self.site_name = name
        self.delay = delay
        self.started_at: float | None = None
        self.seen_keywords: list[str] = []

    def search(
        self, query: str, published_after: datetime, limit: int
    ) -> list[SiteItem]:
        self.started_at = time.monotonic()
        self.seen_keywords.append(query)
        time.sleep(self.delay)
        return [
            SiteItem(
                site=self.site_name,
                query=query,
                title=query,
                url=f"https://{self.site_name}/{query}",
            )
        ]

    def summarize(self, topic: str, item: SiteItem) -> SiteItemSummary:
        return SiteItemSummary(tldr=[item.title])

    def popularity_score(self, item: SiteItem) -> float:
        return 1.0


class TopicOnlyConnector(SlowConnector):
    """A deep-research style connector that ignores keywords entirely."""

    needs_keywords = False

    def search_batch(
        self, topic: str, keywords: list[str], published_after: datetime, limit: int
    ) -> list[SiteItem] | None:
        self.started_at = time.monotonic()
        self.seen_keywords = list(keywords)
        time.sleep(self.delay)
        return [
            SiteItem(
                site=self.site_name,
                query=topic,
                title=topic,
                url=f"https://{self.site_name}/x",
            )
        ]


def _run(researcher: SiteResearcher, keywords, sites: list[str]):
    config = SiteResearchConfig(sites=sites)
    return asyncio.run(researcher.run("topic", keywords, 7, config))


def test_sites_run_concurrently_and_keep_configured_order() -> None:
    a, b, c = SlowConnector("a", 0.3), SlowConnector("b", 0.3), SlowConnector("c", 0.3)
    researcher = SiteResearcher({"a": a, "b": b, "c": c})

    t0 = time.monotonic()
    bundle = _run(researcher, ["k1"], ["c", "a", "b"])
    elapsed = time.monotonic() - t0

    assert elapsed < 0.75, f"sites ran serially ({elapsed:.2f}s)"
    assert list(bundle.items_by_site) == ["c", "a", "b"]
    assert bundle.total_items() == 3
    assert all(
        item.summary for items in bundle.items_by_site.values() for item in items
    )


def test_keyword_free_connector_starts_before_keywords_arrive() -> None:
    async def scenario() -> tuple[float, float, list[str], list[str]]:
        deep = TopicOnlyConnector("deep", 0.2)
        kw = SlowConnector("kw")
        researcher = SiteResearcher({"deep": deep, "kw": kw})
        keywords: asyncio.Future[list[str]] = asyncio.get_running_loop().create_future()
        t0 = time.monotonic()
        task = asyncio.create_task(
            researcher.run(
                "topic", keywords, 7, SiteResearchConfig(sites=["kw", "deep"])
            )
        )
        await asyncio.sleep(0.4)
        keywords.set_result(["late"])
        bundle = await task
        assert bundle.total_items() == 2
        assert deep.started_at is not None and kw.started_at is not None
        return (
            deep.started_at - t0,
            kw.started_at - t0,
            deep.seen_keywords,
            kw.seen_keywords,
        )

    deep_start, kw_start, deep_keywords, kw_keywords = asyncio.run(scenario())
    assert deep_start < 0.2, "topic-only connector waited for keywords"
    assert kw_start >= 0.4, "keyword connector ran before keywords existed"
    assert deep_keywords == []
    assert kw_keywords == ["late"]


def test_no_keywords_skips_keyword_sites_but_runs_topic_only_ones() -> None:
    deep, kw = TopicOnlyConnector("deep"), SlowConnector("kw")
    bundle = _run(SiteResearcher({"deep": deep, "kw": kw}), [], ["kw", "deep"])

    assert list(bundle.items_by_site) == ["deep"]
    assert kw.started_at is None
    assert any("No keywords" in err and "kw" in err for err in bundle.errors)


def test_one_site_crashing_does_not_cost_the_others() -> None:
    class Broken(SlowConnector):
        def is_available(self) -> bool:
            raise RuntimeError("boom")

    bundle = _run(
        SiteResearcher({"ok": SlowConnector("ok"), "bad": Broken("bad")}),
        ["k"],
        ["bad", "ok"],
    )
    assert list(bundle.items_by_site) == ["ok"]
    assert any("bad failed: boom" in err for err in bundle.errors)
