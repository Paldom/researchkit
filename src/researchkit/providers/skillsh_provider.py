"""skills.sh provider — what agents are actually being taught to do.

The Agent Skills directory (https://skills.sh) indexes skills published in
public GitHub repos and ranks them by real install counts. For any topic
about agent tooling that is a signal no web search reaches: not what someone
wrote about a capability, but how many people installed one.

Free and keyless. Install counts are the scoring dimension, mirroring the
upvote-scored Hacker News provider. The directory is a *catalogue*, so this
provider self-gates the same way OpenAlex does: when a topic has no matching
skills it reports that plainly with zero sources rather than padding a
report with unrelated packages.
"""

from __future__ import annotations

import logging
import math
import re
from typing import Any

import requests

from researchkit.network_retry import with_network_retry
from researchkit.providers.base import (
    BaseProvider,
    ProviderResult,
    Source,
    SourceType,
)

SKILLSH_SEARCH_URL = "https://skills.sh/api/search"
SKILLSH_SKILL_URL = "https://skills.sh/"

_MAX_RESULTS = 20
# Kept for compatibility; superseded by the score floor below. A cardinality
# guard cannot work here: measured 2026-08-30, the endpoint returns exactly
# 100 hits for EVERY query — including "xyzzy nonsense qqq", which came back
# with finance skills. Non-emptiness says nothing about relevance.
_MIN_RELEVANT = 2

# The endpoint is `searchType: semantic` (not literal keyword matching), so
# GitHub's per-keyword fan-out is the wrong shape: one well-formed topic query
# already retrieves the neighbourhood that N keyword queries would. Expansion
# is therefore bounded and only uses keywords the USER supplied — synthesized
# keywords are generated after provider collection and never reach us.
_MAX_QUERIES = 4
# A hit carries only id, skillId, name, installs, source: there is NO
# description field to match against, so relevance is judged on the identifier
# strings alone.
_MIN_SCORE = 0.34
# Installs are a global popularity prior, not a query-conditional signal.
# Damped, and weighted far below lexical fit, so a popular unrelated skill
# cannot outrank an exact match with few installs.
_INSTALL_WEIGHT = 0.12
_RANK_WEIGHT = 0.25
_STOPWORDS = frozenset(
    {"the", "a", "an", "for", "and", "or", "of", "to", "in", "on", "with", "how"}
)


logger = logging.getLogger(__name__)


def _terms(text: str) -> set[str]:
    """Meaningful lowercase tokens, singularized crudely for match tolerance."""
    raw = re.split(r"[^a-z0-9]+", text.lower())
    out: set[str] = set()
    for token in raw:
        if len(token) < 3 or token in _STOPWORDS:
            continue
        out.add(token)
        if token.endswith("s") and len(token) > 3:
            out.add(token[:-1])
    return out


def _score(hit: dict[str, Any], terms: set[str], rank: int) -> float:
    """Relevance of one hit, in [0, ~1.4]. No model call, no network.

    Only the identifier strings exist to match on — the API returns no
    description — so lexical fit is measured against name, skillId and the
    source repo, with the repo weighted lowest (a match there often means the
    author's whole collection, not this skill).
    """
    name_terms = _terms(f"{hit.get('name') or ''} {hit.get('skillId') or ''}")
    repo_terms = _terms(str(hit.get("source") or ""))
    if not terms:
        lexical = 0.0
    else:
        strong = len(terms & name_terms) / len(terms)
        weak = len(terms & repo_terms) / len(terms)
        lexical = min(1.0, strong + 0.25 * weak)

    # Hard gate: no query term anywhere in the identifiers means no relevance,
    # whatever the rank or the install count. Without this, a top-ranked
    # popular skill clears the floor on rank + installs alone — measured:
    # "xyzzy nonsense qqq" kept yahoo-finance and trading-analysis.
    if terms and lexical == 0.0:
        return 0.0

    # The endpoint's own ordering is a real signal; decay it over the page.
    rank_score = max(0.0, 1.0 - rank / 40.0)
    installs = math.log1p(max(0, int(hit.get("installs") or 0))) / math.log1p(20_000)
    return lexical + _RANK_WEIGHT * rank_score + _INSTALL_WEIGHT * min(1.0, installs)


class SkillsShProvider(BaseProvider):
    """Agent-skill directory search, scored by installs (keyless)."""

    provider_name = "skillsh"
    model_name = "skills.sh"

    def __init__(
        self,
        sources: set[str] | None = None,
        model: str | None = None,
        keywords: list[str] | None = None,
    ) -> None:
        """Initialize the skills.sh provider (keyless; no api_key)."""
        self.sources = sources or {"social", "web"}
        self.keywords = keywords or []
        if model:
            self.model_name = model

    def _queries(self, topic: str) -> list[str]:
        """The topic, then user keywords — deduped, bounded, topic first."""
        out: list[str] = []
        for candidate in [topic, *self.keywords]:
            text = " ".join(str(candidate or "").split())[:200]
            if text and text.lower() not in {q.lower() for q in out}:
                out.append(text)
            if len(out) >= _MAX_QUERIES:
                break
        return out

    def _search_one(self, query: str) -> list[dict[str, Any]]:
        """One query; the API's own order is its semantic ranking, so keep it."""

        def _get() -> list[dict[str, Any]]:
            resp = requests.get(
                SKILLSH_SEARCH_URL, params={"q": query}, timeout=(10, 60)
            )
            resp.raise_for_status()
            data = resp.json()
            skills = data.get("skills") if isinstance(data, dict) else None
            if not isinstance(skills, list):
                raise ValueError("unexpected skills.sh response shape")
            return [s for s in skills if isinstance(s, dict) and s.get("id")]

        return with_network_retry(
            _get, label="skillsh.search", provider=self.provider_name
        )

    def _search(self, topic: str) -> list[dict[str, Any]]:
        """Query the directory across bounded variations and rank the union.

        Ranking is deterministic and LLM-free: lexical fit against the hit's
        identifiers, the endpoint's own semantic rank, and installs as a damped
        tiebreaker. Recurrence across queries is deliberately NOT a signal — on
        a catalog this small the same popular skills come back for everything,
        so counting repeats would manufacture consensus out of popularity.
        """
        queries = self._queries(topic)
        terms = _terms(" ".join(queries))
        best: dict[str, tuple[dict[str, Any], int]] = {}
        errors = 0
        for query in queries:
            try:
                hits = self._search_one(query)
            except Exception as e:  # one bad query must not lose the others
                errors += 1
                logger.warning("skills.sh query %r failed: %s", query, e)
                continue
            for rank, hit in enumerate(hits):
                key = str(hit.get("id"))
                if key not in best or rank < best[key][1]:
                    best[key] = (hit, rank)

        if not best and errors:
            raise RuntimeError(f"all {errors} skills.sh queries failed")

        scored = [(hit, _score(hit, terms, rank)) for hit, rank in best.values()]
        # A score floor, not a result count: the endpoint always returns 100,
        # so "how many came back" is not evidence of anything.
        kept = [(hit, score) for hit, score in scored if score >= _MIN_SCORE]
        kept.sort(key=lambda pair: pair[1], reverse=True)
        logger.info(
            "skills.sh: %d queries, %d unique hits, %d above the relevance floor",
            len(queries),
            len(best),
            len(kept),
            extra={"stage": "skillsh_ranked", "provider": self.provider_name},
        )
        return [hit for hit, _ in kept[:_MAX_RESULTS]]

    @staticmethod
    def _skill_url(hit: dict[str, Any]) -> str:
        return f"{SKILLSH_SKILL_URL}{str(hit.get('id') or '').lstrip('/')}"

    def _to_sources(self, hits: list[dict[str, Any]]) -> list[Source]:
        sources: list[Source] = []
        seen: set[str] = set()
        for hit in hits:
            url = self._skill_url(hit)
            if url in seen:
                continue
            seen.add(url)
            repo = str(hit.get("source") or "")
            sources.append(
                Source(
                    url=url,
                    title=f"{hit.get('name') or hit.get('skillId')} ({repo})",
                    snippet=f"{int(hit.get('installs') or 0)} installs",
                    source_type=SourceType.WEB,
                )
            )
            if repo:
                gh = f"https://github.com/{repo}"
                if gh not in seen:
                    seen.add(gh)
                    sources.append(
                        Source(url=gh, title=repo, source_type=SourceType.WEB)
                    )
        return sources

    def _format(self, hits: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for hit in hits:
            name = hit.get("name") or hit.get("skillId") or "(unnamed)"
            installs = int(hit.get("installs") or 0)
            repo = hit.get("source") or ""
            lines.append(
                f"- **{name}** — {installs:,} installs, from `{repo}`\n"
                f"  {self._skill_url(hit)}\n"
                f"  Install: `npx skills add {repo}` "
                f"(skill `{hit.get('skillId') or name}`)"
            )
        return "\n".join(lines)

    def fetch_insights(self, topic: str, days: int) -> ProviderResult:
        """Search the skill directory; self-gate when nothing matches.

        ``days`` is accepted for interface parity and ignored: the directory
        exposes cumulative install counts, not a publication date to window on.
        """
        self._log_start()
        try:
            self._log_query("web")
            hits = self._search(topic)
            if len(hits) < _MIN_RELEVANT:
                self._log_done(0, 0)
                return ProviderResult(
                    provider=self.provider_name,
                    model=self.model_name,
                    raw_text=(
                        "# Agent Skills (skills.sh)\n\n*No matching agent "
                        f"skills for this topic ({len(hits)} hits) — nothing "
                        "reported.*"
                    ),
                    sources=[],
                    meta={"skills_found": len(hits), "relevant": False},
                )
            text = (
                "# Agent Skills (skills.sh)\n\n"
                f"{len(hits)} published skills match this topic, ranked by "
                "installs. Install counts measure adoption, not quality or "
                "correctness: they show what people reached for, and are not "
                f"evidence that a skill works.\n\n{self._format(hits)}"
            )
            sources = self._to_sources(hits)
            self._log_done(len(sources), len(text))
            return ProviderResult(
                provider=self.provider_name,
                model=self.model_name,
                raw_text=text,
                sources=sources,
                meta={
                    # Installable procedures, not evidence: kept out of
                    # corroboration and listed as skill_candidates.
                    "evidence": "procedures",
                    "skills_found": len(hits),
                    "relevant": True,
                    "top_installs": int(hits[0].get("installs") or 0),
                    "repos": sorted(
                        {str(h.get("source")) for h in hits if h.get("source")}
                    ),
                },
            )
        except Exception as e:
            return self._create_error_result(f"skills.sh API error: {e}")

    def summarize_result(self, raw_text: str, topic: str) -> str:
        """Metadata API (no LLM of its own): return a truncated listing."""
        return raw_text[:2000] + "..." if len(raw_text) > 2000 else raw_text
