"""OpenAlex provider — the peer-reviewed-literature slice.

OpenAlex indexes scholarly works with DOIs, venues, and citation counts:
citations are literally the product. Free, keyless REST (an optional
``OPENALEX_MAILTO`` joins the polite pool for better rate limits).

Scientific search is only sometimes relevant, so the provider SELF-GATES
deterministically: it always runs the query, and when OpenAlex returns
fewer than a handful of matching works for the topic it reports "scholarly
literature not relevant" with zero sources instead of padding the report
with off-topic papers (the zero-source downgrade then keeps it out of the
cited consensus). Recency: scholarly publishing is slower than the news
cycle, so the lookback window is widened to at least a year.
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Any

import requests

from researchkit.network_retry import with_network_retry
from researchkit.providers.base import (
    BaseProvider,
    ProviderResult,
    Source,
    SourceType,
)

OPENALEX_API_URL = "https://api.openalex.org/works"

_MAX_RESULTS = 15
# ponytail: count-based relevance gate — OpenAlex relevance_score has no
# absolute scale, so "fewer than N matching works" is the honest signal;
# upgrade to score-ratio gating if false positives show up in practice.
_MIN_RELEVANT_WORKS = 3
_MIN_WINDOW_DAYS = 365


def _reconstruct_abstract(inverted: dict[str, list[int]] | None) -> str:
    """Rebuild abstract text from OpenAlex's inverted index."""
    if not inverted:
        return ""
    positions: dict[int, str] = {}
    for word, indexes in inverted.items():
        for i in indexes:
            positions[i] = word
    return " ".join(positions[i] for i in sorted(positions))


class OpenAlexProvider(BaseProvider):
    """Scholarly-works provider with a deterministic relevance gate."""

    provider_name = "openalex"
    model_name = "openalex"

    def __init__(
        self,
        sources: set[str] | None = None,
        model: str | None = None,
    ) -> None:
        """Initialize the OpenAlex provider (keyless; no api_key parameter)."""
        self.sources = sources or {"social", "web"}
        if model:
            self.model_name = model

    def _search(self, topic: str, days: int) -> list[dict[str, Any]]:
        """Query OpenAlex works for the topic within a widened window."""
        window = max(days, _MIN_WINDOW_DAYS)
        since = (dt.date.today() - dt.timedelta(days=window)).isoformat()
        params: dict[str, Any] = {
            "search": topic[:300],
            "filter": f"from_publication_date:{since}",
            "sort": "relevance_score:desc",
            "per-page": _MAX_RESULTS,
        }
        mailto = os.getenv("OPENALEX_MAILTO")
        if mailto:
            params["mailto"] = mailto

        def _get() -> list[dict[str, Any]]:
            resp = requests.get(OPENALEX_API_URL, params=params, timeout=(10, 60))
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results") if isinstance(data, dict) else None
            if not isinstance(results, list):
                raise ValueError("unexpected OpenAlex response shape")
            return [r for r in results if isinstance(r, dict)]

        return with_network_retry(_get, label="openalex.works", provider="openalex")

    @staticmethod
    def _work_url(work: dict[str, Any]) -> str:
        """Best citable URL for a work: DOI, else landing page, else OpenAlex id."""
        doi = work.get("doi") or ""
        if doi:
            return str(doi)
        location = work.get("primary_location") or {}
        landing = location.get("landing_page_url") if isinstance(location, dict) else ""
        return str(landing or work.get("id") or "")

    def _works_to_sources(self, works: list[dict[str, Any]]) -> list[Source]:
        sources: list[Source] = []
        seen: set[str] = set()
        for w in works:
            url = self._work_url(w)
            if not url or url in seen:
                continue
            seen.add(url)
            abstract = _reconstruct_abstract(w.get("abstract_inverted_index"))
            sources.append(
                Source(
                    url=url,
                    title=w.get("display_name"),
                    snippet=abstract[:300] or None,
                    date=w.get("publication_date"),
                    source_type=SourceType.WEB,
                )
            )
        return sources

    @staticmethod
    def _format_works(works: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for w in works:
            title = w.get("display_name") or "Untitled"
            date = w.get("publication_date") or ""
            cited = w.get("cited_by_count") or 0
            location = w.get("primary_location") or {}
            src = location.get("source") if isinstance(location, dict) else None
            venue = (
                (src or {}).get("display_name") or "" if isinstance(src, dict) else ""
            )
            abstract = _reconstruct_abstract(w.get("abstract_inverted_index"))
            meta = ", ".join(x for x in (venue, date, f"cited by {cited}") if x)
            lines.append(f"- **{title}** ({meta})\n  {abstract[:400]}")
        return "\n\n".join(lines)

    def fetch_insights(self, topic: str, days: int) -> ProviderResult:
        """Fetch scholarly works; self-gate when the literature isn't relevant."""
        self._log_start()
        try:
            self._log_query("web")
            works = self._search(topic, days)

            if len(works) < _MIN_RELEVANT_WORKS:
                # Honest no-op: zero sources -> the meta-summary downgrade
                # keeps this out of the cited consensus by design.
                self._log_done(0, 0)
                return ProviderResult(
                    provider=self.provider_name,
                    model=self.model_name,
                    raw_text=(
                        "# Scientific Literature\n\n*Scholarly search judged "
                        f"not relevant for this topic ({len(works)} matching "
                        "works in the window) — no papers reported.*"
                    ),
                    sources=[],
                    meta={"works_found": len(works), "relevant": False},
                )

            sources = self._works_to_sources(works)
            text = f"# Scientific Literature\n\n{self._format_works(works)}"
            self._log_done(len(sources), len(text))
            return ProviderResult(
                provider=self.provider_name,
                model=self.model_name,
                raw_text=text,
                sources=sources,
                meta={"works_found": len(works), "relevant": True},
            )
        except Exception as e:
            return self._create_error_result(f"OpenAlex API error: {e}")

    def summarize_result(self, raw_text: str, topic: str) -> str:
        """OpenAlex is a metadata API (no LLM): return a truncated list."""
        return raw_text[:2000] + "..." if len(raw_text) > 2000 else raw_text
