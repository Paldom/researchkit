"""Tests for the Brave and OpenAlex search providers."""

from __future__ import annotations

from typing import Any

import pytest

import researchkit.providers.brave_provider as brave_mod
import researchkit.providers.openalex_provider as oa_mod
from researchkit.providers.brave_provider import BraveProvider
from researchkit.providers.openalex_provider import (
    OpenAlexProvider,
    _reconstruct_abstract,
)


def _brave_payload(n: int = 2) -> dict[str, Any]:
    return {
        "discussions": {
            "results": [
                {
                    "url": "https://reddit.test/r/x/1",
                    "title": "Thread",
                    "description": "forum take",
                    "page_age": "2026-07-01T00:00:00",
                }
            ]
        },
        "web": {
            "results": [
                {
                    "url": f"https://site.test/{i}",
                    "title": f"Page {i}",
                    "description": "desc",
                    "page_age": "2026-06-15T12:00:00",
                }
                for i in range(n)
            ]
        },
    }


class TestBrave:
    def test_missing_key_is_graceful(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRAVE_API_KEY", raising=False)
        result = BraveProvider().fetch_insights("t", days=7)
        assert not result.is_success and "BRAVE_API_KEY" in (result.error or "")

    def test_dual_query_paces_and_extracts_dated_sources(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sleeps: list[float] = []
        monkeypatch.setattr(brave_mod.time, "sleep", lambda s: sleeps.append(s))
        provider = BraveProvider(api_key="k")
        monkeypatch.setattr(provider, "_search", lambda q, days, rf: _brave_payload())
        result = provider.fetch_insights("agent memory", days=7)
        assert result.is_success
        assert sleeps == [brave_mod._PACE_SECONDS]  # 1 req/s starter pacing
        urls = {s.url for s in result.sources}
        assert "https://reddit.test/r/x/1" in urls  # discussions vertical
        assert all(s.date in ("2026-07-01", "2026-06-15") for s in result.sources)
        assert "Social Media Analysis" in result.raw_text
        assert "Web Research Analysis" in result.raw_text

    def test_freshness_mapping_and_summary_truncation(self) -> None:
        assert BraveProvider._days_to_freshness(1) == "pd"
        assert BraveProvider._days_to_freshness(7) == "pw"
        assert BraveProvider._days_to_freshness(30) == "pm"
        assert BraveProvider._days_to_freshness(90) == "py"
        p = BraveProvider(api_key="k")
        assert p.summarize_result("x" * 3000, "t").endswith("...")
        assert p.summarize_result("short", "t") == "short"

    def test_api_error_becomes_error_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = BraveProvider(api_key="k")

        def boom(q: str, days: int, rf: str) -> dict[str, Any]:
            raise RuntimeError("429 rate limit")

        monkeypatch.setattr(provider, "_search", boom)
        result = provider.fetch_insights("t", days=7)
        assert not result.is_success and "rate limit" in (result.error or "")


def _work(i: int, doi: bool = True) -> dict[str, Any]:
    return {
        "id": f"https://openalex.org/W{i}",
        "doi": f"https://doi.org/10.1/w{i}" if doi else "",
        "display_name": f"Paper {i}",
        "publication_date": "2026-01-15",
        "cited_by_count": 5,
        "primary_location": {
            "landing_page_url": f"https://journal.test/{i}",
            "source": {"display_name": "Journal of Tests"},
        },
        "abstract_inverted_index": {"Agents": [0], "remember": [1], "things": [2]},
    }


class TestOpenAlex:
    def test_abstract_reconstruction(self) -> None:
        assert _reconstruct_abstract({"b": [1], "a": [0]}) == "a b"
        assert _reconstruct_abstract(None) == ""

    def test_relevant_topic_yields_cited_works(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = OpenAlexProvider()
        monkeypatch.setattr(
            provider, "_search", lambda t, d: [_work(i) for i in range(4)]
        )
        result = provider.fetch_insights("agent memory", days=7)
        assert result.is_success and result.meta["relevant"] is True
        assert len(result.sources) == 4
        top = result.sources[0]
        assert top.url.startswith("https://doi.org/")  # DOI preferred
        assert top.date == "2026-01-15"
        assert "Agents remember things" in (top.snippet or "")
        assert "Journal of Tests" in result.raw_text

    def test_irrelevant_topic_self_gates_to_zero_sources(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = OpenAlexProvider()
        monkeypatch.setattr(provider, "_search", lambda t, d: [_work(1)])
        result = provider.fetch_insights("pizza drama", days=7)
        assert result.is_success
        assert result.sources == [] and result.meta["relevant"] is False
        assert "not relevant" in result.raw_text

    def test_doi_fallback_to_landing_page(self) -> None:
        assert (
            OpenAlexProvider._work_url(_work(1, doi=False)) == "https://journal.test/1"
        )

    def test_api_error_becomes_error_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = OpenAlexProvider()

        def boom(t: str, d: int) -> list[dict[str, Any]]:
            raise RuntimeError("503 unavailable")

        monkeypatch.setattr(provider, "_search", boom)
        result = provider.fetch_insights("t", days=7)
        assert not result.is_success and "503" in (result.error or "")

    def test_registry_registration_and_routing(self) -> None:
        from researchkit.plugin_api import ProviderContext
        from researchkit.plugins import get_registry
        from researchkit.plugins_builtin import _make_brave, _make_openalex

        names = get_registry(refresh=True).provider_names
        assert "brave" in names and "openalex" in names
        ctx = ProviderContext(model="", sources=frozenset({"web"}))
        assert isinstance(_make_brave(ctx), BraveProvider)
        assert isinstance(_make_openalex(ctx), OpenAlexProvider)
        assert oa_mod and brave_mod  # imported for parity
