"""Context-aware keyword synthesis from real research findings."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import TYPE_CHECKING

from researchkit.network_retry import with_network_retry

if TYPE_CHECKING:
    from researchkit.providers.base import ProviderResult
    from researchkit.system_config import EffectiveModels

logger = logging.getLogger(__name__)


# Limits to keep the synthesis prompt bounded in size.
MAX_SOURCE_TITLES = 30
MAX_PER_SUMMARY_CHARS = 1500
MAX_META_SUMMARY_CHARS = 2000


def parse_keyword_json(response: str, count: int = 10) -> list[str]:
    """
    Parse keywords from a JSON response string.

    Shared utility used by both individual provider keyword generation
    and the cross-provider synthesis step.  Filters out single-word
    keywords, deduplicates case-insensitively, and caps at *count*.
    """
    text = response.strip()

    data: object = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                data = None

    if not isinstance(data, dict):
        logger.warning(f"parse_keyword_json: failed to parse JSON: {text[:200]}")
        return []

    raw = data.get("keywords")
    if not isinstance(raw, list):
        logger.warning("parse_keyword_json: 'keywords' field missing or not a list")
        return []

    keywords: list[str] = []
    seen: set[str] = set()
    for k in raw:
        s = str(k).strip()
        if not s:
            continue
        if len(s.split()) < 2:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        keywords.append(s)
        if len(keywords) >= count:
            break

    return keywords


class KeywordSynthesizer:
    """
    Synthesizes search keywords from real research findings.

    Supports two modes:

    1. **Single-LLM synthesis** (``synthesize``):  Uses one OpenAI call
       to generate keywords grounded in provider results.  This is the
       legacy/fallback path.
    2. **Multi-provider synthesis** (``synthesize_from_provider_keywords``):
       Takes keyword lists produced independently by each LLM provider and
       merges them into a final list, preferring consensus keywords that
       appear across multiple providers.
    """

    DEFAULT_MODEL = "gpt-5.4-mini"

    def __init__(self, model: str | None = None) -> None:
        """
        Args:
            model: Model name (defaults to DEFAULT_MODEL). Synthesis
                uses the OpenAI API; the improver preset is OpenAI.
        """
        self.model = model or self.DEFAULT_MODEL

    @classmethod
    def from_effective_models(
        cls, effective_models: EffectiveModels | None
    ) -> KeywordSynthesizer:
        """Create a synthesizer using the improver model from effective models."""
        model = effective_models.improver if effective_models else cls.DEFAULT_MODEL
        return cls(model=model)

    # ------------------------------------------------------------------
    # Mode 1 — single-LLM synthesis (legacy / fallback)
    # ------------------------------------------------------------------

    def synthesize(
        self,
        topic: str,
        days: int,
        provider_results: list[ProviderResult],
        individual_summaries: dict[str, str],
        meta_summary: str,
        count: int = 10,
    ) -> list[str]:
        """
        Synthesize search keywords grounded in actual research findings.

        Returns an empty list on failure — callers should handle empty
        keyword lists gracefully (site research already does).

        Args:
            topic: The research topic
            days: Lookback window in days
            provider_results: Provider results to mine for context
            individual_summaries: Per-provider self-summaries
            meta_summary: Cross-provider meta-summary
            count: Maximum number of keywords to return
        """
        context = self._build_context(
            provider_results=provider_results,
            individual_summaries=individual_summaries,
            meta_summary=meta_summary,
        )
        if not context.strip():
            logger.warning(
                "KeywordSynthesizer: empty context, returning no keywords",
                extra={"stage": "keyword_synthesis_empty"},
            )
            return []

        try:
            response = self._call_openai(
                system_prompt=self._system_prompt(),
                user_prompt=self._user_prompt(topic, days, context, count),
            )
        except Exception as e:
            logger.error(
                f"KeywordSynthesizer LLM call failed: {e}",
                extra={"stage": "keyword_synthesis_error"},
            )
            return []

        keywords = self._parse_keywords(response, count)
        logger.info(
            f"KeywordSynthesizer produced {len(keywords)} keywords",
            extra={"stage": "keyword_synthesis_done"},
        )
        return keywords

    # ------------------------------------------------------------------
    # Mode 2 — multi-provider keyword synthesis
    # ------------------------------------------------------------------

    def synthesize_from_provider_keywords(
        self,
        topic: str,
        days: int,
        provider_keywords: dict[str, list[str]],
        count: int = 7,
    ) -> list[str]:
        """
        Synthesize a final keyword list from multiple providers' keyword lists.

        Uses a single LLM call to select, deduplicate, rank and merge
        keywords from all providers into a final list.

        Args:
            topic: The research topic
            days: Lookback window in days
            provider_keywords: Mapping of provider name → keyword list
            count: Target number of final keywords (default: 15)

        Returns:
            Final synthesized keyword list, or empty list on failure.
        """
        if not provider_keywords:
            return []

        # Build context showing each provider's keywords
        context_parts: list[str] = []
        for provider, keywords in provider_keywords.items():
            if keywords:
                keyword_list = "\n".join(f"  - {k}" for k in keywords)
                context_parts.append(f"### {provider}\n{keyword_list}")

        if not context_parts:
            return []

        keyword_context = "\n\n".join(context_parts)

        try:
            response = self._call_openai(
                system_prompt=self._synthesis_system_prompt(),
                user_prompt=self._synthesis_user_prompt(
                    topic, days, keyword_context, count
                ),
            )
        except Exception as e:
            logger.error(
                f"Keyword synthesis LLM call failed: {e}",
                extra={"stage": "keyword_multi_synthesis_error"},
            )
            return self._fallback_merge(provider_keywords, count)

        keywords = parse_keyword_json(response, count)
        if not keywords:
            return self._fallback_merge(provider_keywords, count)

        logger.info(
            f"Multi-provider synthesis produced {len(keywords)} keywords "
            f"from {len(provider_keywords)} providers",
            extra={"stage": "keyword_multi_synthesis_done"},
        )
        return keywords

    # ------------------------------------------------------------------
    # Internals — context building
    # ------------------------------------------------------------------

    def _build_context(
        self,
        provider_results: list[ProviderResult],
        individual_summaries: dict[str, str],
        meta_summary: str,
    ) -> str:
        """Assemble a compact grounding blob from real research findings."""
        parts: list[str] = []

        if meta_summary:
            excerpt = meta_summary[:MAX_META_SUMMARY_CHARS]
            parts.append(f"## Meta-summary\n{excerpt}")

        if individual_summaries:
            summary_lines = ["## Provider summaries"]
            for provider, text in individual_summaries.items():
                if not text:
                    continue
                snippet = text[:MAX_PER_SUMMARY_CHARS]
                summary_lines.append(f"### {provider}\n{snippet}")
            if len(summary_lines) > 1:
                parts.append("\n".join(summary_lines))

        # Top source titles, deduped, capped
        titles: list[str] = []
        seen: set[str] = set()
        for result in provider_results:
            if not result.is_success:
                continue
            for src in result.sources:
                title = (src.title or "").strip()
                if not title:
                    continue
                key = title.lower()
                if key in seen:
                    continue
                seen.add(key)
                titles.append(title)
                if len(titles) >= MAX_SOURCE_TITLES:
                    break
            if len(titles) >= MAX_SOURCE_TITLES:
                break
        if titles:
            parts.append("## Source titles\n" + "\n".join(f"- {t}" for t in titles))

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Internals — prompts
    # ------------------------------------------------------------------

    def _system_prompt(self) -> str:
        return (
            "You are a search keyword specialist. Generate short, practical "
            "search queries grounded in the research findings provided.\n\n"
            "RULES:\n"
            "1. Each keyword should be 2-4 words, plain language.\n"
            "2. Use terms from the findings when relevant, but keep queries "
            "simple — no config file names, version numbers, or deep jargon.\n"
            "3. No duplicates or near-duplicates.\n\n"
            'Return valid JSON: {"keywords": ["...", "..."]}'
        )

    def _user_prompt(self, topic: str, days: int, context: str, count: int) -> str:
        return (
            f"RESEARCH TOPIC: {topic}\n"
            f"TIME WINDOW: last {days} days\n\n"
            f"RESEARCH FINDINGS:\n{context}\n\n"
            f"TASK: Generate {count} short search queries (2-4 words each) "
            "that would help find useful articles, videos, and discussions "
            "about this topic. Keep them simple and broadly useful.\n\n"
            'Return JSON: {"keywords": ["query 1", "query 2", ...]}'
        )

    def _synthesis_system_prompt(self) -> str:
        return (
            "You merge keyword lists from multiple AI providers into one "
            "short, clean list.\n\n"
            "RULES:\n"
            "1. Keep keywords short: 2-4 words each.\n"
            "2. Prefer keywords that appear across multiple providers.\n"
            "3. Remove duplicates and near-duplicates.\n"
            "4. Drop overly niche queries (no package names, version numbers, "
            "or config file paths unless central to the topic).\n"
            "5. Plain language a normal person would search for.\n\n"
            'Return valid JSON: {"keywords": ["query 1", "query 2", ...]}'
        )

    def _synthesis_user_prompt(
        self, topic: str, days: int, keyword_context: str, count: int
    ) -> str:
        return (
            f"RESEARCH TOPIC: {topic}\n"
            f"TIME WINDOW: last {days} days\n\n"
            f"PROVIDER KEYWORD LISTS:\n{keyword_context}\n\n"
            f"TASK: Pick the best {count} search queries from the lists above. "
            "Keep them short (2-4 words), simple, and broadly useful.\n\n"
            'Return JSON: {"keywords": ["query 1", "query 2", ...]}'
        )

    # ------------------------------------------------------------------
    # Internals — LLM call & parsing
    # ------------------------------------------------------------------

    def _call_openai(self, system_prompt: str, user_prompt: str) -> str:
        from openai import OpenAI

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set")

        client = OpenAI(api_key=api_key)
        # Route through the project's unified retry wrapper (backoff + structured
        # logging), consistent with the summarizer. (Review: keyword_synthesizer
        # retry-policy inconsistency.)
        response = with_network_retry(
            client.chat.completions.create,
            label="keyword_synthesizer.chat.completions",
            provider="improver",
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
        )
        return response.choices[0].message.content or ""

    def _parse_keywords(self, response: str, count: int) -> list[str]:
        """Delegate to the module-level parse_keyword_json utility."""
        return parse_keyword_json(response, count)

    @staticmethod
    def _fallback_merge(
        provider_keywords: dict[str, list[str]], count: int
    ) -> list[str]:
        """Fallback: flatten all providers' keywords, dedupe, and take top N."""
        seen: set[str] = set()
        merged: list[str] = []
        for keywords in provider_keywords.values():
            for kw in keywords:
                key = kw.strip().lower()
                if key not in seen and len(kw.split()) >= 2:
                    seen.add(key)
                    merged.append(kw.strip())
                    if len(merged) >= count:
                        return merged
        return merged
