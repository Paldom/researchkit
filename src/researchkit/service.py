"""
Application service layer for Social Research.

This is the single orchestration entry point that all frontends (CLI, web API, MCP)
should use. It coordinates provider collection, summarization, formatting, and saving.
"""

from __future__ import annotations

import contextvars
import logging
import shutil
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from researchkit.aggregator import InsightAggregator, InsightBundle
from researchkit.final_summary import (
    ClaudeFinalSummaryGenerator,
    DigestGenerator,
    ProfessionalOverviewGenerator,
    SuperSummaryGenerator,
)
from researchkit.formatter import format_as_markdown
from researchkit.observability.context import run_context
from researchkit.observability.run_logging import attach_run_file_handler
from researchkit.plugins import default_site_research_sites
from researchkit.project import (
    Project,
    ProjectConfig,
    UserFileSource,
    UserUrlSource,
    create_project,
    create_subproject,
)
from researchkit.prompts import run_context_note_scope
from researchkit.system_config import EffectiveModels, SystemConfigManager
from researchkit.utils import slugify

logger = logging.getLogger(__name__)

ProgressEvent = dict[str, Any]
ProgressCallback = Callable[[ProgressEvent], None]


@dataclass(frozen=True)
class ResearchRequest:
    """Request parameters for a research run."""

    topic: str
    # User-supplied seed keywords for site research (tuple for frozen dataclass).
    # If empty AND site research is enabled, the aggregator will synthesize
    # keywords at run-time grounded in the actual provider findings.
    # Non-empty user keywords always override and skip synthesis.
    keywords: tuple[str, ...] = ()
    days: int = 7
    providers: list[str] = field(
        default_factory=lambda: ["openai", "gemini", "grok", "perplexity"]
    )
    sources: set[str] = field(default_factory=lambda: {"social", "web"})
    include_raw: bool = True
    preset_name: str | None = None  # Model preset to use (None = active preset)
    site_research_enabled: bool = True  # Enable keyword-based site research
    site_research_sites: tuple[str, ...] = ()  # empty = all active connectors
    # User-curated sources, included in final report/article only (never in research).
    user_url_sources: tuple[UserUrlSource, ...] = ()
    user_file_sources: tuple[UserFileSource, ...] = ()
    user_sources_dir: Path | None = None
    # Sibling-awareness note injected into provider prompts when this run is one
    # sub-query of a larger boosted investigation (empty for standalone runs).
    run_context_note: str = ""


@dataclass
class ResearchArtifacts:
    """Results from a research run."""

    run_id: str
    bundle: InsightBundle
    report_markdown: str
    report_json: dict[str, Any]
    report_path: Path | None = None
    log_path: Path | None = None


@dataclass
class BoostedArtifacts:
    """Results from a council-gated, optionally-decomposed (boosted) run."""

    project: Project
    council_result: Any  # council.CouncilResult
    decomposed: bool
    # Populated when decomposed into parallel sub-projects:
    sub_projects: list[Project] = field(default_factory=list)
    sub_artifacts: list[ResearchArtifacts] = field(default_factory=list)
    super_summary_markdown: str | None = None
    # Populated when NOT decomposed (a single improved run):
    single_artifacts: ResearchArtifacts | None = None


def _build_sibling_note(
    parent_topic: str | None,
    own_topic: str,
    sibling_topics: list[str],
) -> str:
    """Build the sibling-awareness note for a boosted sub-project run."""
    if not parent_topic:
        return ""
    siblings = [s for s in sibling_topics if s and s.strip() and s != own_topic]
    lines = [
        f"This research is one sub-query of a larger investigation into: "
        f'"{parent_topic}".',
        f'Your assigned sub-query: "{own_topic}".',
    ]
    if siblings:
        lines.append(
            "Parallel sibling sub-queries (being researched separately): "
            + "; ".join(f'"{s}"' for s in siblings)
            + "."
        )
    return "\n".join(lines)


class SocialResearchService:
    """
    Application/use-case layer for social research.

    This is the ONLY place that should orchestrate:
      - provider collection
      - summarization
      - formatting
      - saving

    All frontends (CLI, web API, MCP) should call this service.
    """

    def __init__(self, projects_dir: Path | None = None) -> None:
        """
        Initialize the service.

        Args:
            projects_dir: Base directory for projects and system config.
                         Defaults to ./projects
        """
        self.projects_dir = projects_dir or Path("projects")
        self._config_manager: SystemConfigManager | None = None

    @property
    def config_manager(self) -> SystemConfigManager:
        """Lazy-load the system config manager."""
        if self._config_manager is None:
            self._config_manager = SystemConfigManager()  # Uses root models.yaml
        return self._config_manager

    def get_effective_models(self, preset_name: str | None = None) -> EffectiveModels:
        """
        Get effective model configuration.

        Args:
            preset_name: Optional preset to use (defaults to active preset)

        Returns:
            EffectiveModels with resolved model versions
        """
        return self.config_manager.resolve_effective_models(preset_name)

    def _generate_final_summary(
        self,
        generator: ClaudeFinalSummaryGenerator,
        bundle: InsightBundle,
        topic: str,
        days: int,
    ) -> str | None:
        """Generate a final Claude-based report summary."""
        return generator.generate(
            meta_summary=bundle.meta_summary,
            individual_summaries=bundle.individual_summaries,
            topic=topic,
            days=days,
            provider_results=bundle.provider_results,
            site_research=bundle.site_research,
            user_url_sources=bundle.user_url_sources,
            user_file_sources=bundle.user_file_sources,
            user_file_contents=bundle.user_file_contents,
        )

    def run(
        self,
        req: ResearchRequest,
        *,
        save: bool = False,
        out_dir: Path = Path("."),
        filename: str | None = None,
        progress: ProgressCallback | None = None,
        log_dir: Path = Path(".logs"),
        log_level: str = "DEBUG",
    ) -> ResearchArtifacts:
        """
        Run a complete research workflow.

        Args:
            req: Research request parameters
            save: Whether to save the report to a file
            out_dir: Directory to save the report (default: current directory)
            filename: Custom filename (default: RESULTS_<topic-slug>.md)
            progress: Optional callback for progress events
            log_dir: Directory for log files (default: .logs)
            log_level: Log level for per-run log file (default: DEBUG)

        Returns:
            ResearchArtifacts containing the bundle, formatted reports, paths, and run_id
        """

        def emit(evt: ProgressEvent) -> None:
            """Emit a progress event: log it and forward to UI callback."""
            stage = evt.get("stage", "-")
            message = evt.get("message", stage)
            extra: dict[str, Any] = {"stage": stage}
            if "provider" in evt:
                extra["provider"] = evt["provider"]
            logger.info(message, extra=extra)
            if progress:
                progress(evt)

        with (
            run_context() as run_id,
            attach_run_file_handler(
                run_id=run_id,
                log_dir=log_dir,
                level=log_level,
            ) as run_log_path,
            run_context_note_scope(req.run_context_note),
        ):
            # The sibling-awareness note is scoped to this run (and reset on
            # exit) so it can't leak into a later run on the same thread.
            # It propagates to the aggregator's async tasks via contextvars.

            # Resolve effective model configuration
            effective_models = self.get_effective_models(req.preset_name)

            emit(
                {
                    "stage": "start",
                    "message": f"Starting research run for topic: {req.topic}",
                    "topic": req.topic,
                    "days": req.days,
                    "run_id": run_id,
                    "preset": effective_models.preset_name,
                }
            )

            aggregator = InsightAggregator(
                sources=set(req.sources),
                effective_models=effective_models,
                site_research_enabled=req.site_research_enabled,
                site_research_sites=list(req.site_research_sites),
            )

            emit(
                {
                    "stage": "collecting",
                    "message": f"Collecting from providers: {', '.join(req.providers)}",
                    "providers": list(req.providers),
                }
            )

            bundle = aggregator.collect_and_summarize_sync(
                topic=req.topic,
                days=req.days,
                providers=req.providers,
                keywords=list(req.keywords),
                progress=emit,
                user_url_sources=list(req.user_url_sources),
                user_file_sources=list(req.user_file_sources),
                user_sources_dir=req.user_sources_dir,
            )

            digest_md: str | None = None
            professional_overview_md: str | None = None

            # Skip the two Claude CLI summary generations entirely when no
            # provider succeeded — otherwise they spend budget summarizing the
            # "*All providers failed*" notice. (Review L2; mirrors the
            # keyword-gen gate in the aggregator.)
            any_success = any(r.is_success for r in bundle.provider_results)

            summary_specs: dict[
                str,
                tuple[str, str, str, ClaudeFinalSummaryGenerator],
            ] = {
                "digest": (
                    "Generating digest summary",
                    "Digest generated",
                    "Digest skipped",
                    DigestGenerator.from_effective_models(effective_models),
                ),
                "professional_overview": (
                    "Generating professional overview",
                    "Professional overview generated",
                    "Professional overview skipped",
                    ProfessionalOverviewGenerator.from_effective_models(
                        effective_models
                    ),
                ),
            }
            if not any_success:
                for stage, spec in summary_specs.items():
                    emit({"stage": f"{stage}_done", "message": spec[2]})
                summary_specs = {}

            future_map: dict[Future[str | None], str] = {}
            with ThreadPoolExecutor(max_workers=max(1, len(summary_specs))) as executor:
                for stage, spec in summary_specs.items():
                    message, _, _, generator = spec
                    emit(
                        {
                            "stage": stage,
                            "message": message,
                        }
                    )
                    # Propagate contextvars (run_id) into the worker so the
                    # final-summary stage's logs land in the per-run log file
                    # instead of being stamped run_id="-" and filtered out.
                    # (Review L3.)
                    ctx = contextvars.copy_context()
                    future = executor.submit(
                        ctx.run,
                        self._generate_final_summary,
                        generator,
                        bundle,
                        req.topic,
                        req.days,
                    )
                    future_map[future] = stage

                for future in as_completed(future_map):
                    stage = future_map[future]
                    _, done_message, skipped_message, _ = summary_specs[stage]
                    try:
                        text = future.result()
                    except Exception as e:
                        logger.warning(
                            f"{stage} generation failed: {e}",
                            extra={"stage": f"{stage}_error"},
                        )
                        text = None

                    if stage == "digest":
                        digest_md = text
                    else:
                        professional_overview_md = text

                    emit(
                        {
                            "stage": f"{stage}_done",
                            "message": done_message if text else skipped_message,
                        }
                    )

            bundle.professional_overview_markdown = professional_overview_md

            emit(
                {
                    "stage": "formatting",
                    "message": "Formatting report",
                }
            )

            md = format_as_markdown(
                bundle,
                include_raw=req.include_raw,
                system_config=effective_models.to_dict() if effective_models else None,
                digest_markdown=digest_md,
            )

            report_path: Path | None = None
            if save:
                out_dir.mkdir(parents=True, exist_ok=True)
                report_path = out_dir / (filename or f"RESULTS_{slugify(req.topic)}.md")
                report_path.write_text(md, encoding="utf-8")

                emit(
                    {
                        "stage": "saved",
                        "message": f"Report saved to {report_path}",
                        "path": str(report_path),
                    }
                )

            # Count successes and failures
            successes = sum(1 for r in bundle.provider_results if r.is_success)
            failures = len(bundle.provider_results) - successes

            emit(
                {
                    "stage": "done",
                    "message": f"Run complete: {successes} succeeded, {failures} failed",
                    "run_id": run_id,
                    "successes": successes,
                    "failures": failures,
                }
            )

            return ResearchArtifacts(
                run_id=run_id,
                bundle=bundle,
                report_markdown=md,
                report_json=bundle.to_dict(),
                report_path=report_path,
                log_path=run_log_path,
            )

    def create_project(
        self,
        topic: str,
        keywords: list[str] | None = None,
        days: int = 7,
        providers: list[str] | None = None,
        sources: list[str] | None = None,
        include_raw: bool = True,
        preset_name: str | None = None,
        site_research_enabled: bool = True,
        site_research_sites: list[str] | None = None,
    ) -> Project:
        """
        Create a new research project with configuration.

        Args:
            topic: Research topic
            keywords: Search keywords to guide research
            days: Lookback window in days
            providers: List of providers to query
            sources: List of sources (social, web)
            include_raw: Include raw provider outputs in report
            preset_name: Model preset to use (defaults to active preset)
            site_research_enabled: Enable keyword-based site research
            site_research_sites: Sites to search (default: exa)

        Returns:
            The created Project
        """
        config = ProjectConfig(
            topic=topic,
            keywords=keywords or [],
            days=days,
            providers=providers or ["openai", "gemini", "grok", "perplexity"],
            sources=sources or ["social", "web"],
            include_raw=include_raw,
            preset_name=preset_name,
            site_research_enabled=site_research_enabled,
            site_research_sites=site_research_sites or default_site_research_sites(),
        )
        return create_project(config, self.projects_dir)

    def run_project(
        self,
        project: Project,
        *,
        progress: ProgressCallback | None = None,
        log_level: str = "DEBUG",
    ) -> ResearchArtifacts:
        """
        Run research for an existing project and save results to the project folder.

        Args:
            project: The project to run
            progress: Optional callback for progress events
            log_level: Log level for per-run log file

        Returns:
            ResearchArtifacts with results saved to project folder
        """
        # Convert project config to research request
        req = ResearchRequest(
            topic=project.config.topic,
            keywords=tuple(project.config.keywords),
            days=project.config.days,
            providers=project.config.providers,
            sources=set(project.config.sources),
            include_raw=project.config.include_raw,
            preset_name=project.config.preset_name,
            site_research_enabled=project.config.site_research_enabled,
            site_research_sites=tuple(project.config.site_research_sites),
            user_url_sources=tuple(project.config.user_url_sources),
            user_file_sources=tuple(project.config.user_file_sources),
            user_sources_dir=project.user_sources_dir,
            run_context_note=_build_sibling_note(
                project.config.parent_topic,
                project.config.topic,
                project.config.sibling_topics,
            ),
        )

        # Run with log in project folder
        artifacts = self.run(
            req,
            save=False,  # We'll save to project folder ourselves
            progress=progress,
            log_dir=project.path,
            log_level=log_level,
        )

        # Save results to project folder
        project.save_results(artifacts.report_json, artifacts.report_markdown)

        # Move the run log to the standard project log location
        if artifacts.log_path and artifacts.log_path.exists():
            shutil.move(str(artifacts.log_path), str(project.log_path))

        # Update artifacts with project paths
        return ResearchArtifacts(
            run_id=artifacts.run_id,
            bundle=artifacts.bundle,
            report_markdown=artifacts.report_markdown,
            report_json=artifacts.report_json,
            report_path=project.report_path,
            log_path=project.log_path,
        )

    def create_and_run_project(
        self,
        topic: str,
        keywords: list[str] | None = None,
        days: int = 7,
        providers: list[str] | None = None,
        sources: list[str] | None = None,
        include_raw: bool = True,
        preset_name: str | None = None,
        site_research_enabled: bool = True,
        site_research_sites: list[str] | None = None,
        progress: ProgressCallback | None = None,
        log_level: str = "DEBUG",
    ) -> tuple[Project, ResearchArtifacts]:
        """
        Create a project and immediately run research (instant mode).

        Args:
            topic: Research topic
            keywords: Search keywords to guide research
            days: Lookback window in days
            providers: List of providers to query
            sources: List of sources (social, web)
            include_raw: Include raw provider outputs in report
            preset_name: Model preset to use (defaults to active preset)
            site_research_enabled: Enable keyword-based site research
            site_research_sites: Sites to search (default: exa)
            progress: Optional callback for progress events
            log_level: Log level for per-run log file

        Returns:
            Tuple of (Project, ResearchArtifacts)
        """
        project = self.create_project(
            topic=topic,
            keywords=keywords,
            days=days,
            providers=providers,
            sources=sources,
            include_raw=include_raw,
            preset_name=preset_name,
            site_research_enabled=site_research_enabled,
            site_research_sites=site_research_sites,
        )

        artifacts = self.run_project(
            project,
            progress=progress,
            log_level=log_level,
        )

        return project, artifacts

    @staticmethod
    def _build_sub_sources_md(bundle: InsightBundle, limit: int = 15) -> str:
        """Build a markdown bullet list of a sub-run's key source links."""
        lines: list[str] = []
        seen: set[str] = set()
        for result in bundle.provider_results:
            if not getattr(result, "is_success", False):
                continue
            for src in getattr(result, "sources", []) or []:
                url = getattr(src, "url", None)
                if not url or url in seen:
                    continue
                seen.add(url)
                title = getattr(src, "title", None) or url
                lines.append(f"- [{title}]({url})")
                if len(lines) >= limit:
                    return "\n".join(lines)
        return "\n".join(lines)

    def _subreport_payload(
        self, subtopic: str, artifacts: ResearchArtifacts
    ) -> dict[str, str]:
        """Build the super-summary input payload for one completed sub-project."""
        bundle = artifacts.bundle
        summary_md = bundle.professional_overview_markdown or bundle.meta_summary or ""
        return {
            "subtopic": subtopic,
            "summary_md": summary_md,
            "sources_md": self._build_sub_sources_md(bundle),
        }

    def create_and_run_boosted(
        self,
        topic: str,
        *,
        days: int = 7,
        providers: list[str] | None = None,
        sources: list[str] | None = None,
        include_raw: bool = True,
        preset_name: str | None = None,
        site_research_enabled: bool = True,
        site_research_sites: list[str] | None = None,
        keyword_count: int = 10,
        force_boost: bool = False,
        progress: ProgressCallback | None = None,
        log_level: str = "DEBUG",
    ) -> BoostedArtifacts:
        """Run an LLM council to improve the topic, then optionally fan out.

        The council always refines the topic and generates keywords. When boost is
        enabled in the active preset AND the council judges the topic worth
        decomposing, the run fans out into parallel sub-projects (each aware of its
        siblings) topped by an opus-authored super-summary. Otherwise it falls back
        to a single improved research run.
        """
        from researchkit.council import LLMCouncil

        effective_models = self.get_effective_models(preset_name)

        def emit(evt: ProgressEvent) -> None:
            logger.info(
                evt.get("message", evt.get("stage", "-")),
                extra={"stage": evt.get("stage", "-")},
            )
            if progress:
                progress(evt)

        # --- Stage 1+2: council deliberation -----------------------------
        emit(
            {
                "stage": "council_start",
                "message": (
                    f"Convening council ({', '.join(effective_models.council_members)}; "
                    f"boss={effective_models.council_boss})"
                ),
            }
        )
        council = LLMCouncil.from_effective_models(effective_models)
        council_result = council.deliberate(topic, count=keyword_count)
        improved_topic = council_result.improved_topic or topic
        keywords = council_result.keywords

        subqueries = council_result.subqueries[: effective_models.boost_max_subprojects]
        should_boost = (
            (effective_models.boost_enabled or force_boost)
            and council_result.decompose
            and len(subqueries) >= 2
        )
        emit(
            {
                "stage": "council_done",
                "message": (
                    f"Council done: decompose={council_result.decompose}, "
                    f"boost={'on' if should_boost else 'off'}, "
                    f"{len(subqueries)} sub-queries, {len(keywords)} keywords"
                ),
            }
        )

        # --- Non-boost path: single improved run -------------------------
        if not should_boost:
            project, artifacts = self.create_and_run_project(
                topic=improved_topic,
                keywords=keywords,
                days=days,
                providers=providers,
                sources=sources,
                include_raw=include_raw,
                preset_name=preset_name,
                site_research_enabled=site_research_enabled,
                site_research_sites=site_research_sites,
                progress=progress,
                log_level=log_level,
            )
            return BoostedArtifacts(
                project=project,
                council_result=council_result,
                decomposed=False,
                single_artifacts=artifacts,
            )

        # --- Boost path: parent project + parallel sub-projects ----------
        parent = self.create_project(
            topic=improved_topic,
            keywords=keywords,
            days=days,
            providers=providers,
            sources=sources,
            include_raw=include_raw,
            preset_name=preset_name,
            site_research_enabled=site_research_enabled,
            site_research_sites=site_research_sites,
        )

        sub_projects: list[Project] = []
        for i, sub_topic in enumerate(subqueries, start=1):
            sub_config = ProjectConfig(
                topic=sub_topic,
                keywords=[],  # each sub-run synthesizes keywords grounded in its findings
                days=days,
                providers=providers or ["openai", "gemini", "grok", "perplexity"],
                sources=sources or ["social", "web"],
                include_raw=include_raw,
                preset_name=preset_name,
                site_research_enabled=site_research_enabled,
                site_research_sites=site_research_sites
                or default_site_research_sites(),
                parent_topic=improved_topic,
                sibling_topics=list(subqueries),
            )
            sub_projects.append(create_subproject(parent, sub_config, i))

        emit(
            {
                "stage": "boost_collecting",
                "message": f"Running {len(sub_projects)} sub-projects in parallel",
                "count": len(sub_projects),
            }
        )

        def run_one(sub: Project) -> ResearchArtifacts:
            def sub_progress(evt: ProgressEvent) -> None:
                if progress:
                    progress({**evt, "subproject": sub.config.topic})

            return self.run_project(sub, progress=sub_progress, log_level=log_level)

        sub_artifacts: list[ResearchArtifacts] = []
        with ThreadPoolExecutor(max_workers=len(sub_projects)) as executor:
            future_to_sub = {executor.submit(run_one, s): s for s in sub_projects}
            for future in as_completed(future_to_sub):
                sub = future_to_sub[future]
                try:
                    sub_artifacts.append(future.result())
                except Exception as e:
                    logger.warning(
                        f"Sub-project failed ({sub.config.topic}): {e}",
                        extra={"stage": "boost_subproject_error"},
                    )

        # --- Super-summary across sub-projects ---------------------------
        emit(
            {
                "stage": "super_summary_start",
                "message": f"Generating super-summary (model={effective_models.council_boss})",
            }
        )
        # Pair artifacts back to their sub-topics in decomposition order.
        topic_by_path = {s.path: s.config.topic for s in sub_projects}
        payloads: list[dict[str, str]] = []
        for art in sub_artifacts:
            subtopic = next(
                (
                    t
                    for p, t in topic_by_path.items()
                    if art.report_path and p in art.report_path.parents
                ),
                "Sub-investigation",
            )
            payloads.append(self._subreport_payload(subtopic, art))

        super_md: str | None = None
        try:
            generator = SuperSummaryGenerator.from_effective_models(effective_models)
            super_md = generator.generate_super_summary(improved_topic, days, payloads)
        except Exception as e:
            logger.warning(
                f"Super-summary generation failed: {e}",
                extra={"stage": "super_summary_error"},
            )

        if super_md:
            parent.save_super_summary(super_md)
        parent.save_results(
            {
                "decomposed": True,
                "overarching_topic": improved_topic,
                "council": council_result.to_dict(),
                "subprojects": [s.name for s in sub_projects],
                "super_summary": super_md,
            },
            super_md or "# Super-summary unavailable\n",
        )
        emit(
            {
                "stage": "super_summary_done",
                "message": "Super-summary generated"
                if super_md
                else "Super-summary skipped",
            }
        )

        return BoostedArtifacts(
            project=parent,
            council_result=council_result,
            decomposed=True,
            sub_projects=sub_projects,
            sub_artifacts=sub_artifacts,
            super_summary_markdown=super_md,
        )

    def add_user_source(
        self,
        project: Project,
        source: str,
        title: str | None = None,
        note: str | None = None,
    ) -> UserUrlSource | UserFileSource:
        """
        Add a user-curated source (URL or local file path) to a project.

        Auto-detects URL vs file: strings starting with http:// or https://
        are treated as URLs; everything else is treated as a local file path
        and copied into projects/<name>/user_sources/.

        Returns the registered source. Raises ValueError for invalid URLs and
        FileNotFoundError for missing files.
        """
        from urllib.parse import urlparse

        added: UserUrlSource | UserFileSource
        stripped = source.strip()
        parsed = urlparse(stripped)
        # If the input looks URL-shaped (has a scheme + netloc), require http(s).
        if parsed.scheme and parsed.netloc:
            if parsed.scheme.lower() not in {"http", "https"}:
                raise ValueError(
                    f"Invalid URL scheme '{parsed.scheme}': only http and https are supported"
                )
            entry = UserUrlSource(url=stripped, title=title, note=note)
            if not project.add_user_url_source(entry):
                logger.info(f"Duplicate user URL source ignored: {entry.url}")
            added = entry
        else:
            src_path = Path(stripped).expanduser().resolve()
            added = project.add_user_file_source(src_path, title=title, note=note)

        project.save_config()
        return added

    def remove_user_source(self, project: Project, identifier: str) -> bool:
        """
        Remove a user source by URL, filename, or 1-based index from list_user_sources.

        Returns True if removal occurred.
        """
        removed = project.remove_user_source(identifier)
        if removed:
            project.save_config()
        return removed

    def list_user_sources(
        self,
        project: Project,
    ) -> tuple[list[UserUrlSource], list[UserFileSource]]:
        """Return (url_sources, file_sources) for the given project."""
        return (
            list(project.config.user_url_sources),
            list(project.config.user_file_sources),
        )
