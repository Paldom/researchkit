"""Aggregator for concurrent provider queries and result collection."""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from researchkit.keyword_synthesizer import KeywordSynthesizer
from researchkit.plugin_api import ProviderContext
from researchkit.plugins import Registry, get_registry
from researchkit.project import UserFileSource, UserUrlSource
from researchkit.providers import BaseProvider, ProviderResult
from researchkit.summarizer import Summarizer

if TYPE_CHECKING:
    from researchkit.site_research import SiteResearchBundle
    from researchkit.system_config import EffectiveModels

# Read budgets for user-supplied document files (kept small so we never
# blow the article-prompt context). Files are read-once into the bundle
# and only used for the final-article LLM, never for provider research.
_USER_FILE_PER_FILE_MAX = 200_000  # bytes
_USER_FILE_TOTAL_MAX = 600_000  # bytes
_USER_FILE_TEXT_EXTENSIONS = {".md", ".markdown", ".txt", ".rst", ".text"}

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict[str, Any]], None]


@dataclass
class InsightBundle:
    """Complete bundle of social insights with summaries."""

    topic: str
    keywords: list[str]  # User-supplied seed keywords (may be empty)
    days: int
    providers_queried: list[str]
    meta_summary: str
    provider_results: list[ProviderResult]
    individual_summaries: dict[str, str] = field(default_factory=dict)
    professional_overview_markdown: str | None = None
    provider_models: dict[str, str] = field(default_factory=dict)
    system_config_used: dict[str, Any] = field(default_factory=dict)
    site_research: SiteResearchBundle | None = (
        None  # Keyword-based site research results
    )
    synthesized_keywords: list[str] | None = (
        None  # Keywords synthesized from real findings
    )
    provider_keywords: dict[str, list[str]] | None = None  # Per-provider keyword lists
    # User-curated sources, included in final report/article only.
    user_url_sources: list[UserUrlSource] = field(default_factory=list)
    user_file_sources: list[UserFileSource] = field(default_factory=list)
    # File contents loaded at run time; not serialized to result.json.
    user_file_contents: dict[str, str] = field(
        default_factory=dict, repr=False, compare=False
    )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result = {
            "topic": self.topic,
            "keywords": self.keywords,
            "days": self.days,
            "providers_queried": self.providers_queried,
            "meta_summary": self.meta_summary,
            "provider_results": [
                {
                    "provider": r.provider,
                    "model": r.model,
                    "raw_text": r.raw_text,
                    "sources": [s.to_dict() for s in r.sources],
                    "meta": r.meta,
                    "error": r.error,
                }
                for r in self.provider_results
            ],
            "individual_summaries": self.individual_summaries,
        }
        if self.professional_overview_markdown is not None:
            result["professional_overview_markdown"] = (
                self.professional_overview_markdown
            )
        # Add provider models and system config if present
        if self.provider_models:
            result["provider_models"] = self.provider_models
        if self.system_config_used:
            result["system_config_used"] = self.system_config_used
        # Add site research results if present
        if self.site_research:
            result["site_research"] = self.site_research.to_dict()
        # Add synthesized keywords if present
        if self.synthesized_keywords is not None:
            result["synthesized_keywords"] = self.synthesized_keywords
        # Add per-provider keyword lists if present
        if self.provider_keywords is not None:
            result["provider_keywords"] = self.provider_keywords
        # User-curated sources (URL list and file metadata; file contents are
        # NOT serialized — they exist on disk under projects/<name>/user_sources/).
        if self.user_url_sources:
            result["user_url_sources"] = [s.to_dict() for s in self.user_url_sources]
        if self.user_file_sources:
            result["user_file_sources"] = [s.to_dict() for s in self.user_file_sources]
        return result


# Keyword-capable ("LLM") providers are declared per-spec (ProviderSpec.is_llm)
# in the plugin registry — see researchkit.plugins_builtin for the defaults.


class InsightAggregator:
    """
    Aggregates social insights from multiple providers concurrently.

    Handles:
    - Concurrent provider queries via asyncio
    - Individual result summarization
    - Meta-summary generation across all providers
    - Multi-provider keyword generation and synthesis
    - Progress callbacks for status updates
    """

    def __init__(
        self,
        summarizer: Summarizer | None = None,
        sources: set[str] | None = None,
        effective_models: EffectiveModels | None = None,
        site_research_enabled: bool = True,
        site_research_sites: list[str] | None = None,
        registry: Registry | None = None,
    ) -> None:
        """
        Initialize the aggregator.

        Args:
            summarizer: Summarizer instance (creates default if None)
            sources: Set of sources to query ("social", "web", or both)
            effective_models: Model configuration from system config
            site_research_enabled: Whether to enable keyword-based site research
            site_research_sites: Sites to search (default: exa)
        """
        self.effective_models = effective_models
        self.registry = registry or get_registry()
        self.site_research_enabled = site_research_enabled
        from researchkit.plugins import default_site_research_sites

        self.site_research_sites = list(
            site_research_sites or default_site_research_sites()
        )
        # Create summarizer with model override if effective_models provided
        summarizer_model = effective_models.summarizer if effective_models else None
        self.summarizer = summarizer or Summarizer(
            model=summarizer_model,
        )
        self.sources = sources or {"social", "web"}

    def _builtin_options(self, name: str) -> dict[str, Any]:
        """Synthesize the legacy preset knobs into the builtin's options dict."""
        em = self.effective_models
        if em is None:
            return {}
        knobs: dict[str, dict[str, Any]] = {
            "openai": {"reasoning_effort": em.reasoning_effort},
            "perplexity": {"search_type": em.perplexity_search_type},
            "tavily": {"search_depth": em.tavily_search_depth},
            "claude": {
                "max_budget": em.claude_max_budget,
                "reasoning_effort": em.reasoning_effort,
            },
            "github": {"improver_model": em.improver},
        }
        return knobs.get(name, {})

    def _create_provider(
        self,
        name: str,
        keywords: list[str] | None = None,
    ) -> BaseProvider:
        """Create a provider through its registered spec (builtin or plugin)."""
        spec = self.registry.providers.get(name)
        if spec is None:
            raise ValueError(f"Unknown provider: {name}")

        em = self.effective_models
        model = ""
        options: dict[str, Any] = {}
        if em is not None:
            builtin_model = getattr(em, name, None)
            model = str(
                builtin_model or em.plugin_models.get(name) or spec.default_model or ""
            )
            options = {
                **self._builtin_options(name),
                **em.plugin_options.get(name, {}),
            }
        ctx = ProviderContext(
            model=model,
            sources=frozenset(self.sources),
            keywords=tuple(keywords or ()),
            options=options,
        )
        return spec.factory(ctx)

    async def _fetch_from_provider_async(
        self,
        provider_name: str,
        topic: str,
        days: int,
        keywords: list[str] | None = None,
    ) -> ProviderResult:
        """Fetch insights from a single provider asynchronously."""
        provider = self._create_provider(provider_name, keywords=keywords)
        # Run the synchronous fetch in a thread pool
        # asyncio.to_thread propagates contextvars, so run_id is available
        return await asyncio.to_thread(provider.fetch_insights, topic, days)

    async def _run_site_research(
        self,
        topic: str,
        keywords: list[str],
        days: int,
        progress: ProgressCallback | None = None,
    ) -> SiteResearchBundle | None:
        """
        Run keyword-based site research (Exa).

        Args:
            topic: The research topic
            keywords: Keywords to search for
            days: Lookback window in days
            progress: Optional progress callback

        Returns:
            SiteResearchBundle with results, or None if disabled/failed
        """
        if not self.site_research_enabled or not keywords:
            return None

        from researchkit.site_research import SiteResearchConfig, create_site_researcher

        if progress:
            progress(
                {
                    "stage": "site_research_start",
                    "message": f"Starting site research: {', '.join(self.site_research_sites)}",
                    "sites": self.site_research_sites,
                }
            )

        logger.info(
            f"Starting site research: sites={self.site_research_sites}, keywords={len(keywords)}",
            extra={"stage": "site_research_start"},
        )

        try:
            # Get site_summarizer model from effective_models
            summarizer_model = "gemini-3-flash-preview"
            if self.effective_models:
                summarizer_model = self.effective_models.site_summarizer

            researcher = create_site_researcher(
                sites=self.site_research_sites,
                summarizer_model=summarizer_model,
                plugin_options=(
                    self.effective_models.plugin_options
                    if self.effective_models
                    else None
                ),
            )

            config = SiteResearchConfig(
                enabled=True,
                sites=self.site_research_sites,
            )

            bundle = await researcher.run(
                topic=topic,
                keywords=keywords,
                days=days,
                config=config,
            )

            if progress:
                progress(
                    {
                        "stage": "site_research_done",
                        "message": f"Site research complete: {bundle.total_items()} items",
                        "total_items": bundle.total_items(),
                        "errors": len(bundle.errors),
                    }
                )

            logger.info(
                f"Site research complete: {bundle.total_items()} items, {len(bundle.errors)} errors",
                extra={"stage": "site_research_done"},
            )

            return bundle

        except Exception as e:
            logger.warning(
                f"Site research failed: {e}",
                extra={"stage": "site_research_error"},
            )
            if progress:
                progress(
                    {
                        "stage": "site_research_error",
                        "message": f"Site research failed: {e}",
                    }
                )
            return None

    async def _generate_keywords_from_providers(
        self,
        topic: str,
        days: int,
        provider_results: list[ProviderResult],
        individual_summaries: dict[str, str],
        meta_summary: str,
        progress: ProgressCallback | None = None,
    ) -> dict[str, list[str]]:
        """
        Generate keywords from each LLM provider concurrently.

        Each successful LLM provider independently produces top-10 keywords
        for the topic, grounded in the research findings collected so far.

        Args:
            topic: Research topic
            days: Lookback window
            provider_results: Results from Phase 1 (used for context building)
            individual_summaries: Per-provider summaries
            meta_summary: Cross-provider meta-summary
            progress: Optional progress callback

        Returns:
            Dict mapping provider name → keyword list
        """
        # Build compact grounding context from real findings
        synthesizer = KeywordSynthesizer.from_effective_models(self.effective_models)
        context = synthesizer._build_context(
            provider_results=provider_results,
            individual_summaries=individual_summaries,
            meta_summary=meta_summary,
        )

        # Only ask LLM providers that succeeded in Phase 1
        successful_llm_providers = [
            r.provider
            for r in provider_results
            if r.is_success and r.provider in self.registry.llm_provider_names()
        ]

        if not successful_llm_providers:
            return {}

        async def _generate_for_provider(
            provider_name: str,
        ) -> tuple[str, list[str]]:
            provider = self._create_provider(provider_name)
            keywords = await asyncio.to_thread(
                provider.generate_keywords, topic, days, context
            )
            return provider_name, keywords

        tasks = [
            asyncio.create_task(_generate_for_provider(name))
            for name in successful_llm_providers
        ]

        provider_keywords: dict[str, list[str]] = {}
        for future in asyncio.as_completed(tasks):
            try:
                name, keywords = await future
                if keywords:
                    provider_keywords[name] = keywords
                    logger.info(
                        f"Provider {name} generated {len(keywords)} keywords",
                        extra={
                            "stage": "keyword_gen_done",
                            "provider": name,
                        },
                    )
                if progress:
                    progress(
                        {
                            "stage": "keyword_gen_provider_done",
                            "message": f"{name}: {len(keywords)} keywords",
                            "provider": name,
                            "count": len(keywords),
                        }
                    )
            except Exception as e:
                logger.warning(
                    f"Keyword generation failed for provider: {e}",
                    extra={"stage": "keyword_gen_error"},
                )

        return provider_keywords

    async def collect_async(
        self,
        topic: str,
        days: int,
        providers: Sequence[str],
        progress: ProgressCallback | None = None,
        keywords: list[str] | None = None,
    ) -> list[ProviderResult]:
        """
        Collect insights from multiple providers concurrently.

        Uses as_completed to emit progress as each provider finishes.

        Args:
            topic: The topic to research
            days: Number of days to look back
            providers: List of provider names to query
            progress: Optional callback for progress events
            keywords: Optional user-supplied keywords (forwarded to keyword-based providers)

        Returns:
            List of ProviderResults from all providers
        """
        valid_providers = [
            name for name in providers if name in self.registry.providers
        ]
        total = len(valid_providers)

        if not valid_providers:
            return []

        if progress:
            progress(
                {
                    "stage": "providers_start",
                    "message": f"Starting {total} provider(s)",
                    "total": total,
                    "providers": valid_providers,
                }
            )

        # Wrap each fetch so it always returns (name, result) and never raises —
        # asyncio.as_completed yields NEW awaitables (not the original tasks), so
        # a name captured on the coroutine is the only reliable way to attribute a
        # failure. The old code left the name as "unknown", stored the error under
        # that key, and then dropped it from the returned list, silently hiding a
        # crashed provider and undercounting failures. (Review L1.)
        async def _fetch_named(name: str) -> tuple[str, ProviderResult]:
            try:
                return name, await self._fetch_from_provider_async(
                    name, topic, days, keywords=keywords
                )
            except Exception as e:
                logger.exception(
                    "Provider failed with exception",
                    extra={"stage": "provider_error", "provider": name},
                )
                return name, ProviderResult(
                    provider=name, model="unknown", raw_text="", error=str(e)
                )

        tasks: list[asyncio.Task] = []
        for name in valid_providers:
            logger.info(
                f"Starting provider: {name}",
                extra={"stage": "provider_start", "provider": name},
            )
            if progress:
                progress(
                    {
                        "stage": "provider_start",
                        "message": f"Starting {name}",
                        "provider": name,
                    }
                )
            tasks.append(asyncio.create_task(_fetch_named(name), name=f"fetch_{name}"))

        # Process results as they complete
        results_by_name: dict[str, ProviderResult] = {}

        for done_count, future in enumerate(asyncio.as_completed(tasks), start=1):
            provider_name, result = await future
            ok = result.is_success
            sources_count = len(result.sources) if ok else 0
            chars = len(result.raw_text) if ok else 0

            logger.info(
                f"Provider {provider_name} completed: {'success' if ok else 'failed'}, "
                f"{sources_count} sources, {chars} chars",
                extra={
                    "stage": "provider_done",
                    "provider": provider_name,
                },
            )
            results_by_name[provider_name] = result

            if progress:
                progress(
                    {
                        "stage": "provider_done",
                        "message": f"{provider_name}: {'success' if ok else 'failed'}",
                        "provider": provider_name,
                        "ok": ok,
                        "done": done_count,
                        "total": total,
                        "sources": sources_count,
                    }
                )

        # Return results in original provider order
        return [
            results_by_name[name] for name in valid_providers if name in results_by_name
        ]

    def _load_user_file_contents(
        self,
        user_file_sources: Sequence[UserFileSource],
        user_sources_dir: Path | None,
    ) -> dict[str, str]:
        """
        Read user-supplied document files from disk into a {filename: text} map.

        Only used as additional context for the final-article LLM. Never enters
        provider research, summarizer, meta-summary, keyword synthesis, or site
        research. Per-file and total byte budgets are enforced.
        """
        if not user_file_sources or user_sources_dir is None:
            return {}
        if not user_sources_dir.exists():
            return {}

        contents: dict[str, str] = {}
        total = 0
        for entry in user_file_sources:
            path = user_sources_dir / entry.filename
            if not path.exists() or not path.is_file():
                logger.warning(
                    f"User source file missing on disk, skipping: {path}",
                    extra={"stage": "user_sources_skip"},
                )
                continue
            ext = path.suffix.lower()
            if ext not in _USER_FILE_TEXT_EXTENSIONS:
                logger.warning(
                    f"Unsupported user source file extension '{ext}', skipping: {path}",
                    extra={"stage": "user_sources_skip"},
                )
                continue
            try:
                raw = path.read_bytes()
            except OSError as e:
                logger.warning(
                    f"Failed to read user source file {path}: {e}",
                    extra={"stage": "user_sources_skip"},
                )
                continue

            truncated = False
            if len(raw) > _USER_FILE_PER_FILE_MAX:
                raw = raw[:_USER_FILE_PER_FILE_MAX]
                truncated = True

            remaining = _USER_FILE_TOTAL_MAX - total
            if remaining <= 0:
                logger.warning(
                    f"User source file budget exhausted, skipping {path}",
                    extra={"stage": "user_sources_skip"},
                )
                break
            if len(raw) > remaining:
                raw = raw[:remaining]
                truncated = True

            try:
                text = raw.decode("utf-8", errors="replace")
            except Exception as e:
                logger.warning(
                    f"Failed to decode user source file {path}: {e}",
                    extra={"stage": "user_sources_skip"},
                )
                continue

            if truncated:
                text += "\n\n[truncated]\n"
                logger.info(
                    f"User source file truncated to fit budget: {path.name}",
                    extra={"stage": "user_sources_truncated"},
                )

            contents[entry.filename] = text
            total += len(raw)

        return contents

    async def collect_and_summarize_async(
        self,
        topic: str,
        days: int,
        providers: Sequence[str],
        keywords: list[str] | None = None,
        progress: ProgressCallback | None = None,
        user_url_sources: Sequence[UserUrlSource] | None = None,
        user_file_sources: Sequence[UserFileSource] | None = None,
        user_sources_dir: Path | None = None,
    ) -> InsightBundle:
        """
        Collect insights from all providers and generate summaries.

        Flow:
            1. Run providers (concurrently) on the topic alone.
            2. Score results, generate per-provider summaries, meta-summary.
            3. If no user-supplied keywords AND site research is enabled,
               synthesize keywords grounded in the real findings.
            4. Run site research with the chosen keywords.

        User-supplied keywords always take precedence and skip synthesis.

        Args:
            topic: The topic to research
            days: Number of days to look back
            providers: List of provider names to query
            keywords: Optional user-supplied seed keywords. If empty, the
                aggregator will synthesize keywords from real findings
                before running site research.
            progress: Optional callback for progress events

        Returns:
            InsightBundle with all results and summaries
        """
        user_keywords = list(keywords or [])

        # Phase 1: Run providers (concurrently)
        # User keywords are forwarded to keyword-based providers (e.g. github).
        provider_results = await self.collect_async(
            topic,
            days,
            providers,
            progress,
            keywords=user_keywords or None,
        )

        # Generate individual summaries using provider self-summarization
        if progress:
            progress(
                {
                    "stage": "summarizing",
                    "message": "Generating individual summaries (provider self-summarization)",
                }
            )

        logger.info(
            "Starting individual summarization (provider self-summarization)",
            extra={"stage": "summarizing"},
        )

        individual_summaries: dict[str, str] = {}
        successful_results = [r for r in provider_results if r.is_success]

        if successful_results:
            # Use provider self-summarization: each provider summarizes its own results
            async def _summarize_with_provider(
                result: ProviderResult,
            ) -> tuple[str, str]:
                """Summarize using the provider's own model."""
                provider = self._create_provider(result.provider)
                summary = await asyncio.to_thread(
                    provider.summarize_result, result.raw_text, topic
                )
                return result.provider, summary

            summary_tasks = [
                asyncio.create_task(_summarize_with_provider(result))
                for result in successful_results
            ]

            for task in asyncio.as_completed(summary_tasks):
                try:
                    provider_name, summary = await task
                    individual_summaries[provider_name] = summary
                except Exception:
                    logger.exception(
                        "Failed to summarize provider result",
                        extra={"stage": "summarize_error"},
                    )
                    # Fallback: provider self-summarization failed, error is logged

        # Generate meta-summary
        if progress:
            progress(
                {
                    "stage": "meta_summarizing",
                    "message": "Generating meta-summary",
                }
            )

        logger.info(
            "Starting meta-summarization",
            extra={"stage": "meta_summarizing"},
        )

        meta_summary = await asyncio.to_thread(
            self.summarizer.create_meta_summary,
            topic,
            days,
            provider_results,
        )

        logger.info(
            "Summarization complete",
            extra={"stage": "summarize_done"},
        )

        # Phase 3: Multi-provider keyword generation + synthesis.
        # User-supplied keywords always override and skip synthesis.
        synthesized_keywords: list[str] | None = None
        collected_provider_keywords: dict[str, list[str]] | None = None
        effective_keywords = user_keywords

        if (
            not user_keywords
            and self.site_research_enabled
            and any(r.is_success for r in provider_results)
        ):
            if progress:
                progress(
                    {
                        "stage": "keyword_generation_start",
                        "message": "Generating keywords from multiple providers",
                    }
                )
            logger.info(
                "Starting multi-provider keyword generation",
                extra={"stage": "keyword_generation_start"},
            )

            # Phase 3a: Each LLM provider generates keywords concurrently
            collected_provider_keywords = await self._generate_keywords_from_providers(
                topic=topic,
                days=days,
                provider_results=provider_results,
                individual_summaries=individual_summaries,
                meta_summary=meta_summary,
                progress=progress,
            )

            # Phase 3b: Synthesize the best keywords from all providers
            synthesizer = KeywordSynthesizer.from_effective_models(
                self.effective_models
            )

            if collected_provider_keywords:
                if progress:
                    progress(
                        {
                            "stage": "keyword_synthesis_start",
                            "message": (
                                f"Synthesizing keywords from "
                                f"{len(collected_provider_keywords)} providers"
                            ),
                        }
                    )

                synthesized_keywords = await asyncio.to_thread(
                    synthesizer.synthesize_from_provider_keywords,
                    topic,
                    days,
                    collected_provider_keywords,
                    7,
                )
            else:
                # Fallback: no provider generated keywords, use single-LLM
                logger.warning(
                    "No providers generated keywords, falling back to "
                    "single-LLM synthesis",
                    extra={"stage": "keyword_gen_fallback"},
                )
                synthesized_keywords = await asyncio.to_thread(
                    synthesizer.synthesize,
                    topic,
                    days,
                    provider_results,
                    individual_summaries,
                    meta_summary,
                )

            effective_keywords = synthesized_keywords or []

            if progress:
                progress(
                    {
                        "stage": "keyword_synthesis_done",
                        "message": (f"Synthesized {len(effective_keywords)} keywords"),
                        "count": len(effective_keywords),
                    }
                )

        # Phase 4: Run site research with the chosen keywords.
        site_research_bundle = await self._run_site_research(
            topic, effective_keywords, days, progress
        )

        # Extract actual models used from provider results
        provider_models = {
            r.provider: r.model for r in provider_results if r.is_success
        }
        # Add summarizer model
        provider_models["summarizer"] = self.summarizer.model

        # Build system config snapshot
        system_config_used = {}
        if self.effective_models:
            system_config_used = self.effective_models.to_dict()
        # Plugin provenance: record exactly which plugin code ran (§2 of the
        # plugin trust model — provenance over promises).
        active_plugins = self.registry.plugin_versions()
        if active_plugins:
            system_config_used["plugins"] = active_plugins

        # Attach user-curated sources (URLs + file metadata + file contents).
        # These never reach providers/summarizer/site_research; they only flow
        # into the formatter and the final-article LLM.
        url_list = list(user_url_sources or [])
        file_list = list(user_file_sources or [])
        file_contents = self._load_user_file_contents(file_list, user_sources_dir)

        return InsightBundle(
            topic=topic,
            keywords=user_keywords,
            days=days,
            providers_queried=list(providers),
            meta_summary=meta_summary,
            provider_results=provider_results,
            individual_summaries=individual_summaries,
            provider_models=provider_models,
            system_config_used=system_config_used,
            site_research=site_research_bundle,
            synthesized_keywords=synthesized_keywords,
            provider_keywords=collected_provider_keywords,
            user_url_sources=url_list,
            user_file_sources=file_list,
            user_file_contents=file_contents,
        )

    def _run_async(self, coro):
        """
        Run an async coroutine in a sync context.

        Handles the case where an event loop is already running (e.g., in
        Jupyter notebooks or async hosts) by using nest_asyncio if available,
        or falling back to running in a new thread with context propagation.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No running loop, safe to use asyncio.run
            return asyncio.run(coro)

        # A loop is already running on this thread (Jupyter / async host). Run the
        # coroutine in a fresh thread with its own event loop rather than
        # monkey-patching the host loop via nest_asyncio, which mutates global
        # state for the whole process. Copy the context so run_id and the
        # sibling-awareness note propagate. (Review L4.)
        import concurrent.futures

        ctx = contextvars.copy_context()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(ctx.run, asyncio.run, coro).result()

    def collect_sync(
        self,
        topic: str,
        days: int,
        providers: Sequence[str],
        progress: ProgressCallback | None = None,
        keywords: list[str] | None = None,
    ) -> list[ProviderResult]:
        """
        Synchronous wrapper for collect_async.

        Args:
            topic: The topic to research
            days: Number of days to look back
            providers: List of provider names to query
            progress: Optional callback for progress events
            keywords: Optional user-supplied keywords (forwarded to keyword-based providers)

        Returns:
            List of ProviderResults from all providers
        """
        return self._run_async(
            self.collect_async(topic, days, providers, progress, keywords=keywords)
        )

    def collect_and_summarize_sync(
        self,
        topic: str,
        days: int,
        providers: Sequence[str],
        keywords: list[str] | None = None,
        progress: ProgressCallback | None = None,
        user_url_sources: Sequence[UserUrlSource] | None = None,
        user_file_sources: Sequence[UserFileSource] | None = None,
        user_sources_dir: Path | None = None,
    ) -> InsightBundle:
        """
        Synchronous wrapper for collect_and_summarize_async.

        Args:
            topic: The topic to research
            days: Number of days to look back
            providers: List of provider names to query
            keywords: Optional search keywords to guide research
            progress: Optional callback for progress events
            user_url_sources: Optional user-curated URLs (final report/article only)
            user_file_sources: Optional user-curated file references (article context only)
            user_sources_dir: Directory holding the user-supplied files

        Returns:
            InsightBundle with all results and summaries
        """
        return self._run_async(
            self.collect_and_summarize_async(
                topic,
                days,
                providers,
                keywords,
                progress,
                user_url_sources=user_url_sources,
                user_file_sources=user_file_sources,
                user_sources_dir=user_sources_dir,
            )
        )
