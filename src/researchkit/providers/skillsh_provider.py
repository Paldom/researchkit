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
# The directory's search is fuzzy and always returns *something*; below this
# many hits the matches are noise rather than a signal about the topic.
_MIN_RELEVANT = 2


class SkillsShProvider(BaseProvider):
    """Agent-skill directory search, scored by installs (keyless)."""

    provider_name = "skillsh"
    model_name = "skills.sh"

    def __init__(
        self,
        sources: set[str] | None = None,
        model: str | None = None,
    ) -> None:
        """Initialize the skills.sh provider (keyless; no api_key)."""
        self.sources = sources or {"social", "web"}
        if model:
            self.model_name = model

    def _search(self, topic: str) -> list[dict[str, Any]]:
        """Query the directory and rank by install count."""

        def _get() -> list[dict[str, Any]]:
            resp = requests.get(
                SKILLSH_SEARCH_URL, params={"q": topic[:200]}, timeout=(10, 60)
            )
            resp.raise_for_status()
            data = resp.json()
            skills = data.get("skills") if isinstance(data, dict) else None
            if not isinstance(skills, list):
                raise ValueError("unexpected skills.sh response shape")
            return [s for s in skills if isinstance(s, dict) and s.get("id")]

        hits = with_network_retry(
            _get, label="skillsh.search", provider=self.provider_name
        )
        hits.sort(key=lambda s: int(s.get("installs") or 0), reverse=True)
        return hits[:_MAX_RESULTS]

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
