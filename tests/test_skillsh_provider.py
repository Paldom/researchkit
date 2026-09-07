"""Tests for the skills.sh directory provider's retrieval and ranking.

The endpoint is semantic and ALWAYS returns 100 hits — measured 2026-08-30,
including for "xyzzy nonsense qqq", which came back with finance skills. So
relevance here is a scoring problem, not a counting one, and these tests pin
the behaviour that keeps noise out.
"""

from __future__ import annotations

from typing import Any

import pytest

from researchkit.providers.skillsh_provider import SkillsShProvider


def _hit(
    skill_id: str, installs: int = 0, source: str = "owner/pack"
) -> dict[str, Any]:
    # The real API returns exactly these fields — notably NO description.
    return {
        "id": f"{source}/{skill_id}",
        "skillId": skill_id,
        "name": skill_id,
        "installs": installs,
        "source": source,
    }


class TestQueryExpansion:
    def test_topic_first_then_user_keywords_deduped_and_bounded(self) -> None:
        provider = SkillsShProvider(
            keywords=["video summarization", "youtube transcript", "a", "b", "c", "d"]
        )
        queries = provider._queries("youtube transcript")
        assert queries[0] == "youtube transcript"
        # The duplicate keyword is dropped, and expansion stays bounded.
        assert queries.count("youtube transcript") == 1
        assert len(queries) <= 4

    def test_no_keywords_means_a_single_query(self) -> None:
        # The endpoint is semantic: one well-formed topic already retrieves the
        # neighbourhood, so there is nothing to gain from inventing queries.
        assert SkillsShProvider()._queries("kubernetes operator") == [
            "kubernetes operator"
        ]


class TestRanking:
    def _search(self, monkeypatch: pytest.MonkeyPatch, pages: dict[str, list], **kw):
        provider = SkillsShProvider(**kw)
        monkeypatch.setattr(
            SkillsShProvider, "_search_one", lambda self, q: pages.get(q, [])
        )
        return provider._search("youtube transcript")

    def test_lexical_fit_outranks_a_far_more_popular_stranger(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = [
            _hit("yahoo-finance", installs=99_000),
            _hit("youtube-transcript", installs=12),
        ]
        kept = self._search(monkeypatch, {"youtube transcript": page})
        # Install count is a global prior, not a query-conditional signal.
        assert [h["skillId"] for h in kept] == ["youtube-transcript"]

    def test_zero_lexical_overlap_is_rejected_whatever_its_rank(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Rank 0 + huge installs previously cleared the score floor on their
        # own; a hit matching nothing in the query is not a result.
        page = [_hit("trading-analysis", installs=50_000)]
        assert self._search(monkeypatch, {"youtube transcript": page}) == []

    def test_recurrence_across_queries_is_not_corroboration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The same popular skill comes back for every query on a catalog this
        # small; counting repeats would manufacture consensus from popularity.
        popular = _hit("youtube-downloader", installs=80_000)
        exact = _hit("youtube-transcript", installs=5)
        pages = {
            "youtube transcript": [popular, exact],
            "subtitles": [popular],
            "captions": [popular],
        }
        kept = self._search(monkeypatch, pages, keywords=["subtitles", "captions"])
        assert kept[0]["skillId"] == "youtube-transcript"

    def test_best_rank_across_queries_wins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = _hit("youtube-transcript")
        pages = {
            "youtube transcript": [_hit("youtube-other")] * 30 + [target],
            "transcript": [target],  # rank 0 here
        }
        kept = self._search(monkeypatch, pages, keywords=["transcript"])
        assert kept[0]["skillId"] == "youtube-transcript"


class TestFailureHandling:
    def test_one_failing_query_does_not_lose_the_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def flaky(self: Any, query: str) -> list[dict[str, Any]]:
            if query == "boom":
                raise RuntimeError("502")
            return [_hit("youtube-transcript")]

        monkeypatch.setattr(SkillsShProvider, "_search_one", flaky)
        kept = SkillsShProvider(keywords=["boom"])._search("youtube transcript")
        assert [h["skillId"] for h in kept] == ["youtube-transcript"]

    def test_every_query_failing_raises_rather_than_reporting_nothing_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An empty list would be indistinguishable from "the directory has
        # nothing on this topic", which is a different and reportable fact.
        def boom(self: Any, query: str) -> list[dict[str, Any]]:
            raise RuntimeError("503")

        monkeypatch.setattr(SkillsShProvider, "_search_one", boom)
        with pytest.raises(RuntimeError, match=r"skills\.sh"):
            SkillsShProvider()._search("anything")
