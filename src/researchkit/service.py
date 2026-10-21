"""
Application service layer for Social Research.

This is the single orchestration entry point that all frontends (CLI, web API, MCP)
should use. It coordinates provider collection, summarization, formatting, and saving.
"""

from __future__ import annotations

import contextvars
import dataclasses
import json
import logging
import shutil
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from researchkit.aggregator import InsightAggregator, InsightBundle
from researchkit.final_summary import (
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
from researchkit.publish import Publication, publish, render_failure_article
from researchkit.run_limit import run_slot
from researchkit.system_config import EffectiveModels, SystemConfigManager
from researchkit.utils import slugify

if TYPE_CHECKING:
    from researchkit.providers.base import ProviderResult

logger = logging.getLogger(__name__)

# Providers a run queries when the caller names none.
DEFAULT_PROVIDERS = ("openai", "gemini", "grok", "perplexity")

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
    providers: list[str] = field(default_factory=lambda: list(DEFAULT_PROVIDERS))
    sources: set[str] = field(default_factory=lambda: {"social", "web"})
    include_raw: bool = True
    preset_name: str | None = None  # Model preset to use (None = active preset)
    site_research_enabled: bool = True  # Enable keyword-based site research
    # Shared brain this run feeds and starts from (PLAN §4b); None = none.
    brain: Path | None = None
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
    # Handoff writers that ran alongside the overview and digest. None when the
    # writer was skipped, failed, or (skill) declined for thin research.
    article_markdown: str | None = None
    skill_markdown: str | None = None
    # Honest outcome (see run_status): "ok" / "degraded" / "failed".
    status: str = "ok"
    # Archive-first publication outputs (None/empty on the legacy path).
    evidence_markdown: str | None = None
    providers_markdown: str | None = None
    claims: list[dict[str, Any]] = field(default_factory=list)


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


def _super_summary_unavailable_report(reason: str | None) -> str:
    """Report body for a boosted run whose super-summary never came back."""
    return (
        "# Super-summary unavailable\n\n"
        f"The super-summary writer (Claude Code CLI) failed: {reason or 'unknown'}\n\n"
        "The sub-project reports under `subprojects/` are complete; "
        "fix the cause above and re-run to get the integrated article.\n"
    )


_CLAUDE_AUTH_TTL_S = 600.0
_claude_auth_cache: tuple[float, bool | None, str] | None = None


def claude_cli_login_problem() -> str | None:
    """Why the Claude Code CLI cannot write for us right now, or None.

    Every run ends in Claude Code writers, so a logged-out CLI turns a whole
    run into raw provider dumps. `claude auth status` is local and free.
    """
    # ponytail: 10-min cache; a server process lives for days and auth
    # changes under it, while a test or CLI run must not probe per call.
    global _claude_auth_cache
    import time

    now = time.monotonic()
    if _claude_auth_cache and now - _claude_auth_cache[0] < _CLAUDE_AUTH_TTL_S:
        _, logged_in, detail = _claude_auth_cache
    else:
        if shutil.which("claude") is None:
            # Doctor reports a missing binary; here it would only make the
            # run-start events differ between machines (CI has no claude).
            logged_in, detail = None, "`claude` not found on PATH"
        else:
            from researchkit.doctor import claude_auth_status

            logged_in, detail = claude_auth_status()
        _claude_auth_cache = (now, logged_in, detail)
    if logged_in is False:
        return (
            f"Claude Code CLI: {detail}. The overview/digest/article/skill "
            "writers, the council boss and the super-summary will all fail "
            "(or, on an API route, bill the API instead of the subscription)."
        )
    return None


def run_status(
    provider_results: Sequence[ProviderResult],
    article: str | None,
    materials: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The run's honest outcome, recorded in result.json and the exit code.

    ``failed``: no slot returned a single source, so there is nothing cited.
    ``degraded``: something failed (a slot hit quota, timed out, errored), a
    slot was cut off at its deadline (``partial``), or the article is missing. ``ok`` otherwise. A slot that answered without
    sources is ``empty``: routine for keyless providers, so it is reported but
    does not degrade the run on its own.
    """
    slots = {r.provider: r.slot_state for r in provider_results}
    # A slot salvaged at its deadline (``partial``) still cites what it read.
    with_sources = sum(1 for r in provider_results if r.is_success and r.sources)
    partial = sorted(n for n, state in slots.items() if state == "partial")
    failed_slots = sorted(
        n for n, state in slots.items() if state not in ("complete", "empty", "partial")
    )
    reasons: list[str] = []
    if with_sources == 0:
        reasons.append("no provider returned sources")
    if partial:
        reasons.append("cut off at the deadline: " + ", ".join(partial))
    if failed_slots:
        reasons.append(
            "failed slots: " + ", ".join(f"{n} ({slots[n]})" for n in failed_slots)
        )
    if not article:
        reasons.append("article missing")
    # Archive-first runs: citations with nothing archived can't support a claim.
    archived_none = (
        materials is not None and with_sources and not materials.get("fetched")
    )
    if archived_none:
        reasons.append("no cited source could be archived")
    status = (
        "failed"
        if with_sources == 0 or archived_none
        else ("degraded" if reasons else "ok")
    )
    return {
        "status": status,
        "reasons": reasons,
        "slots": slots,
        "with_sources": with_sources,
    }


def _stale_partial_reason(result: ProviderResult, slots: EffectiveModels) -> str | None:
    """Why a saved provider result must not be reused, or None to reuse it.

    Only successful answers are worth keeping: a failure (quota, timeout)
    replayed on resume just repeats an outage that may be over. A result
    produced by a different model than the preset now pins is stale too.
    """
    if not result.is_success:
        return f"previous attempt failed ({result.slot_state})"
    spec = getattr(slots, result.provider, None)
    if isinstance(spec, str) and ":" in spec and not spec.startswith("deep"):
        base = spec.split("@", 1)[0]
        if result.model not in (spec, base):
            return f"model changed ({result.model} -> {base})"
    return None


def _archive(project: Project, payload: dict[str, Any], limit: int) -> dict[str, Any]:
    """Archive a run's cited sources from its in-memory result (PLAN 2.4)."""
    from researchkit.materials import download_materials

    return download_materials(project, result=payload, limit=limit)


def refuse_api_billed_llm(em: EffectiveModels, providers: Iterable[str]) -> None:
    """Raise before any spend if a harness_only preset would bill an LLM API."""
    if bad := em.api_billed_llm_slots(providers):
        raise ValueError(
            f"Preset {em.preset_name!r} is harness_only, but these LLM steps "
            f"would bill an API: {', '.join(bad)}. Give them a CLI spec "
            "(codex:, agy:, grokcli:, kimicli:, claude:) or drop the provider; "
            "search APIs (perplexity, tavily, brave, exa) are fine."
        )


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

    def resolve_run_defaults(
        self,
        preset_name: str | None,
        providers: list[str] | None,
        site_research_enabled: bool | None,
    ) -> tuple[list[str], bool]:
        """Providers and site research for a run: caller > preset > built-in.

        Harness presets declare their zero-key provider set and site research
        off, so ``--preset harness-fast`` alone is the subscription-only,
        time-boxed run (the CLI, MCP and web UI no longer hard-code it).
        """
        em = self.get_effective_models(preset_name)
        chosen = list(providers or em.providers or DEFAULT_PROVIDERS)
        site = site_research_enabled
        if site is None:
            site = True if em.site_research is None else em.site_research
        return chosen, site

    @staticmethod
    def _enrich_with_repos(
        repo_dir: Path,
        bundle: InsightBundle,
        emit: ProgressCallback,
        *,
        keep_clones: bool = False,
    ) -> list[str]:
        """Harvest the repos this run cited; return compact cards for the prompt.

        Bounded and fail-open by construction: the providers' answers are
        already paid for and on disk by the time this runs, so nothing here is
        allowed to raise or to run past its deadline. Partial results are
        reported plainly rather than passed off as complete — a report that
        quietly dropped its grounding reads as more authoritative than it is.
        """
        from researchkit.repos import (
            extract_repo_urls,
            extract_repos_for_run,
            render_repo_card,
        )

        result = bundle.to_dict()
        wanted = extract_repo_urls(result)
        if not wanted:
            return []

        emit(
            {
                "stage": "repos_start",
                "message": f"Harvesting {len(wanted)} cited repositor(y/ies)",
                "count": len(wanted),
            }
        )
        try:
            repos = extract_repos_for_run(repo_dir, result, keep_clones=keep_clones)
        except Exception as e:  # defence in depth; the helper already swallows
            logger.warning("repo enrichment failed outright: %s", e)
            repos = []

        usable = [r for r in repos if r.ok]
        bundle.repo_extracts = [
            {
                "slug": r.slug,
                "url": r.url,
                "extract_file": r.extract_file,
                "docs": len(r.docs),
                "declarations": len(r.api),
                "clone_deleted": r.deleted,
                "error": r.error,
            }
            for r in repos
        ]
        missing = len(wanted) - len(usable)
        emit(
            {
                "stage": "repos_done",
                "message": (
                    f"Repository grounding: {len(usable)}/{len(wanted)} harvested"
                    + (f", {missing} unavailable in time" if missing else "")
                ),
                "harvested": len(usable),
                "requested": len(wanted),
            }
        )
        return [render_repo_card(r) for r in usable]

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
        partial_sink: Callable[[Any], None] | None = None,
        archive: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        preloaded_results: dict[str, Any] | None = None,
        repos_enabled: bool = True,
        repo_dir: Path | None = None,
        repo_keep_clones: bool = False,
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
            partial_sink: Called with each ProviderResult as it lands, so an
                interrupted run keeps what it already paid for.
            preloaded_results: Provider results restored from an interrupted
                run; those providers are not queried again.
            repos_enabled: Clone and harvest the GitHub repos this run cites,
                feeding compact cards to the final analysis (default on).
            repo_dir: Project directory to write ``repos/`` into. Without one
                there is nowhere to keep the extracts, so the step is skipped.
            repo_keep_clones: Keep the cloned source trees on disk.

        Returns:
            ResearchArtifacts containing the bundle, formatted reports, paths, and run_id
        """

        # Per-stage timeline persisted in result.json (PLAN 1.14): seconds
        # since run start on a monotonic clock, one entry per progress event.
        # Without it the critical path can only be guessed from logs.
        started = time.monotonic()
        timeline: list[dict[str, Any]] = []

        def emit(evt: ProgressEvent) -> None:
            """Emit a progress event: log it and forward to UI callback."""
            stage = evt.get("stage", "-")
            entry: dict[str, Any] = {
                "stage": stage,
                "t_s": round(time.monotonic() - started, 2),
            }
            if "provider" in evt:
                entry["provider"] = evt["provider"]
            timeline.append(entry)
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
            # Held for the whole run: every frontend reaches providers through
            # here, so this is the one place a machine-wide ceiling can hold.
            run_slot(topic=req.topic, run_id=run_id, progress=emit),
        ):
            # The sibling-awareness note is scoped to this run (and reset on
            # exit) so it can't leak into a later run on the same thread.
            # It propagates to the aggregator's async tasks via contextvars.

            # Resolve effective model configuration
            effective_models = self.get_effective_models(req.preset_name)
            refuse_api_billed_llm(effective_models, req.providers)

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
            if auth_problem := claude_cli_login_problem():
                logger.error(auth_problem, extra={"stage": "claude_cli_auth_error"})
                if progress:
                    progress(
                        {"stage": "claude_cli_auth_error", "message": auth_problem}
                    )

            aggregator = InsightAggregator(
                sources=set(req.sources),
                effective_models=effective_models,
                site_research_enabled=req.site_research_enabled,
                site_research_sites=list(req.site_research_sites),
                partial_sink=partial_sink,
                preloaded_results=preloaded_results,
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

            # --- Repository enrichment -------------------------------------
            # Between collection and final analysis: the providers' findings
            # are already bought and saved by the partial sink, and the digest
            # and overview below are the analysis that should see the repos.
            # Hard-bounded, never raises — a slow clone degrades the grounding,
            # it does not cost the report. (Design reviewed by the flagship
            # council: bound it, never block on it, keep the bulk on disk.)
            # Runs beside the archive download below (PLAN 3.4): the two are
            # independent network work, so the writers wait for the slower
            # one, not for both in turn. Joined before publication.
            archive_payload = bundle.to_dict()  # snapshot before repos mutate it
            repo_job: Future[list[str]] | None = None
            repo_pool: ThreadPoolExecutor | None = None
            if repos_enabled and repo_dir is not None:
                repo_pool = ThreadPoolExecutor(max_workers=1)
                repo_job = repo_pool.submit(
                    contextvars.copy_context().run,
                    self._enrich_with_repos,
                    repo_dir,
                    bundle,
                    emit,
                    keep_clones=repo_keep_clones,
                )

            # --- Archive first (PLAN 2.4) ------------------------------------
            # Cited sources are downloaded BEFORE the writers, so the archive
            # exists when the report is written and checked, and a brain built
            # later sees every source. Never raises: a failed archive degrades
            # the run status, it does not cost the paid research.
            materials_summary: dict[str, Any] | None = None
            if archive is not None and any(
                r.is_success for r in bundle.provider_results
            ):
                emit({"stage": "materials_start", "message": "Archiving cited sources"})
                try:
                    manifest = archive(archive_payload)
                    materials_summary = {
                        "fetched": manifest.get("fetched", 0),
                        "total_cited": manifest.get("total_cited", 0),
                    }
                    emit(
                        {
                            "stage": "materials_done",
                            "message": (
                                f"Archived {materials_summary['fetched']} of "
                                f"{materials_summary['total_cited']} cited sources"
                            ),
                        }
                    )
                except Exception as e:
                    logger.warning(
                        f"Archiving failed: {e}", extra={"stage": "materials_error"}
                    )
                    materials_summary = {
                        "fetched": 0,
                        "total_cited": 0,
                        "error": str(e),
                    }

            repo_cards: list[str] = []
            if repo_job is not None and repo_pool is not None:
                repo_cards = repo_job.result()
                repo_pool.shutdown()

            # --- Publication (PLAN 2.4-2.11) ---------------------------------
            written: dict[str, str | None] = {}
            publication: Publication | None = None
            any_success = any(r.is_success for r in bundle.provider_results)
            prior: list[tuple[str, str, str]] = []
            if req.brain is not None and any_success:
                from researchkit.brains import prior_knowledge

                hits = prior_knowledge(req.brain, req.topic)
                prior = [(h.url, h.title, h.snippet) for h in hits]
                emit(
                    {
                        "stage": "brain_context",
                        "message": f"Brain {req.brain.name}: {len(prior)} related sources already known",
                    }
                )
            if any_success:
                publication = publish(
                    topic=req.topic,
                    days=req.days,
                    bundle=bundle,
                    project_dir=repo_dir,
                    effective_models=effective_models,
                    run_id=run_id,
                    emit=emit,
                    repo_cards=repo_cards,
                    prior=prior,
                )
                md = publication.report_markdown
                written["article"] = publication.article_markdown
                # Back-compat: the overview field now carries the lead.
                bundle.professional_overview_markdown = publication.lead
            else:
                md = format_as_markdown(bundle, include_raw=req.include_raw)

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

            status = run_status(
                bundle.provider_results, written.get("article"), materials_summary
            )
            if status["status"] == "failed" and not written.get("article"):
                # Never an LLM on an empty trace (gpt-researcher #1204): an
                # honest stdlib article saying what failed.
                written["article"] = render_failure_article(req.topic, status)
            states = list(status["slots"].values())
            successes = states.count("complete")
            empty = states.count("empty")
            failures = len(states) - successes - empty
            emit(
                {
                    "stage": "done",
                    "message": (
                        f"Run {status['status']}: {successes} with sources, "
                        f"{empty} empty, {failures} failed"
                        + (
                            f" — {'; '.join(status['reasons'])}"
                            if status["reasons"]
                            else ""
                        )
                    ),
                    "run_id": run_id,
                    "successes": successes,
                    "failures": failures,
                    "status": status["status"],
                }
            )
            report_json = bundle.to_dict()
            report_json["run_status"] = status
            report_json["timings"] = timeline
            if materials_summary is not None:
                report_json["materials"] = materials_summary
            if publication is not None:
                report_json["publication"] = publication.summary()
                report_json["citations"] = [
                    {"n": c.n, "url": c.url, "title": c.title, "status": c.status}
                    for c in publication.sources
                ]
            # skills.sh-style hits go to skillskit as candidates, not evidence.
            report_json["skill_candidates"] = [
                s.url
                for r in bundle.provider_results
                if r.meta.get("evidence") == "procedures"
                for s in r.sources
            ]

            return ResearchArtifacts(
                run_id=run_id,
                bundle=bundle,
                report_markdown=md,
                report_json=report_json,
                report_path=report_path,
                log_path=run_log_path,
                article_markdown=written.get("article"),
                skill_markdown=written.get("skill"),
                status=status["status"],
                evidence_markdown=publication.evidence_markdown
                if publication
                else None,
                providers_markdown=publication.providers_markdown
                if publication
                else None,
                claims=publication.claims if publication else [],
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
        site_research_enabled: bool | None = None,
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
        providers, site_research_enabled = self.resolve_run_defaults(
            preset_name, providers, site_research_enabled
        )
        config = ProjectConfig(
            topic=topic,
            keywords=keywords or [],
            days=days,
            providers=providers,
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
        resume: bool = True,
        repos_enabled: bool = True,
        repo_keep_clones: bool = False,
        materials: bool = True,
        materials_limit: int = 0,
        brain: Path | None = None,
    ) -> ResearchArtifacts:
        """
        Run research for an existing project and save results to the project folder.

        Provider results are written to ``partial/`` as they land, so a run that
        is killed keeps everything it already paid for; re-running the project
        reuses them instead of buying the same answers twice.

        Args:
            project: The project to run
            progress: Optional callback for progress events
            log_level: Log level for per-run log file
            resume: Reuse provider results left by an interrupted run (default
                on — they cost money). Pass False to re-query everything.
            repos_enabled: Harvest the GitHub repos this run cites into
                ``repos/`` and feed compact cards to the final analysis.
            repo_keep_clones: Keep the cloned source trees on disk.

        Returns:
            ResearchArtifacts with results saved to project folder
        """
        from researchkit.providers.base import ProviderResult

        preloaded: dict[str, ProviderResult] = {}
        if resume:
            slots = self.get_effective_models(project.config.preset_name)
            for name, payload in project.load_partials().items():
                try:
                    result = ProviderResult.from_dict(payload)
                except (KeyError, TypeError, ValueError) as e:
                    logger.warning(f"Ignoring unusable partial for {name}: {e}")
                    continue
                if reason := _stale_partial_reason(result, slots):
                    logger.info(
                        f"Re-running {name} instead of reusing its partial: {reason}",
                        extra={"stage": "partial_rerun", "provider": name},
                    )
                    continue
                preloaded[name] = result
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
            brain=brain,
        )

        # Run with log in project folder
        artifacts = self.run(
            req,
            save=False,  # We'll save to project folder ourselves
            progress=progress,
            log_dir=project.path,
            log_level=log_level,
            partial_sink=lambda result: project.save_partial(
                result.provider, result.to_dict()
            ),
            archive=(
                (lambda payload: _archive(project, payload, materials_limit))
                if materials
                else None
            ),
            preloaded_results=preloaded,
            repos_enabled=repos_enabled,
            repo_dir=project.path,
            repo_keep_clones=repo_keep_clones,
        )

        # Save in dependency order: the derived artifacts first (a rerun that
        # produced none removes the stale ones), result.json — the completion
        # marker — last.
        project.save_article(artifacts.article_markdown)
        project.save_skill(artifacts.skill_markdown)
        project.save_publication(
            evidence_markdown=artifacts.evidence_markdown,
            providers_markdown=artifacts.providers_markdown,
            claims=artifacts.claims,
        )
        project.save_results(artifacts.report_json, artifacts.report_markdown)
        if brain is not None and artifacts.status != "failed":
            # After result.json (brainkit gates ingest on it). Never raises:
            # a busy or broken brain must not cost a finished, paid run.
            from researchkit.brains import ingest_into

            try:
                outcome: dict[str, Any] = ingest_into(brain, project.path)
            except Exception as e:
                outcome = {"brain": str(brain), "error": str(e)[:300]}
            artifacts.report_json["brain"] = outcome
            project.save_results(artifacts.report_json, artifacts.report_markdown)
        # result.json now supersedes the per-provider partials.
        project.clear_partials()

        # Move the run log to the standard project log location
        if artifacts.log_path and artifacts.log_path.exists():
            shutil.move(str(artifacts.log_path), str(project.log_path))

        # Same artifacts with project paths. dataclasses.replace keeps every
        # field (status included: a field-by-field rebuild once dropped it,
        # so the CLI exit code always read "ok").
        return dataclasses.replace(
            artifacts, report_path=project.report_path, log_path=project.log_path
        )

    def republish(
        self,
        project: Project,
        *,
        progress: ProgressCallback | None = None,
    ) -> ResearchArtifacts:
        """Re-run only the publication stage on a finished project.

        Same research, new report/article/claims: no provider is queried
        again. Archives the cited sources first when ``materials/`` is
        missing. This is how report-writing changes are A/B'd on frozen
        evidence, and how an older run gets the archive-first report.
        """
        from researchkit.materials import download_materials
        from researchkit.providers.base import ProviderResult

        result = project.load_results()
        if result is None:
            raise FileNotFoundError(
                f"{project.result_path} not found: run the project first"
            )
        emit: Callable[[dict[str, Any]], None] = progress or (lambda evt: None)
        if not (project.path / "materials" / "index.json").is_file():
            emit({"stage": "materials_start", "message": "Archiving cited sources"})
            download_materials(project, result=result)
        bundle = InsightBundle(
            topic=str(result.get("topic") or project.config.topic),
            keywords=list(result.get("keywords") or []),
            days=int(result.get("days") or project.config.days),
            providers_queried=list(result.get("providers_queried") or []),
            meta_summary=str(result.get("meta_summary") or ""),
            provider_results=[
                ProviderResult.from_dict(r)
                for r in result.get("provider_results") or []
            ],
            individual_summaries=dict(result.get("individual_summaries") or {}),
            provider_models=dict(result.get("provider_models") or {}),
            system_config_used=dict(result.get("system_config_used") or {}),
        )
        effective_models = self.get_effective_models(project.config.preset_name)
        publication = publish(
            topic=bundle.topic,
            days=bundle.days,
            bundle=bundle,
            project_dir=project.path,
            effective_models=effective_models,
            run_id=str(result.get("run_id") or project.name),
            emit=emit,
        )
        result["professional_overview_markdown"] = publication.lead
        result["publication"] = publication.summary()
        result["citations"] = [
            {"n": c.n, "url": c.url, "title": c.title, "status": c.status}
            for c in publication.sources
        ]
        status = run_status(
            bundle.provider_results,
            publication.article_markdown,
            result.get("materials"),
        )
        result["run_status"] = status
        article = publication.article_markdown
        if status["status"] == "failed" and not article:
            article = render_failure_article(bundle.topic, status)
        project.save_article(article)
        project.save_publication(
            evidence_markdown=publication.evidence_markdown,
            providers_markdown=publication.providers_markdown,
            claims=publication.claims,
        )
        project.save_results(result, publication.report_markdown)
        return ResearchArtifacts(
            run_id=str(result.get("run_id") or project.name),
            bundle=bundle,
            report_markdown=publication.report_markdown,
            report_json=result,
            report_path=project.report_path,
            article_markdown=article,
            status=status["status"],
            evidence_markdown=publication.evidence_markdown,
            providers_markdown=publication.providers_markdown,
            claims=publication.claims,
        )

    def make_skill(self, project: Project) -> str | None:
        """Write skill/SKILL.md on request from a finished project (PLAN 2.14).

        Runs no longer distill a skill by default (self-generated skills scored
        8-11 points below no skill, digest 6; skillskit owns the validated
        pack -> skill rewrite). The writer sees the analysis plus the run's
        verified claims, so each instruction can carry its claim's hedges and
        dates. Returns the skill text, or None when the writer declined.
        """
        from researchkit.final_summary import SkillGenerator
        from researchkit.providers.base import ProviderResult

        result = project.load_results()
        if result is None:
            raise FileNotFoundError(
                f"{project.result_path} not found: run the project first"
            )
        claims_text = ""
        if project.claims_path.is_file():
            rows = [
                json.loads(line)
                for line in project.claims_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            claims_text = "\n".join(
                f"- {r['sentence']} ({r['canonical_url']})" for r in rows[:200]
            )
        meta = str(result.get("meta_summary") or "")
        if claims_text:
            meta += (
                "\n\n## Verified claims (verbatim, with their sources)\n\n"
                + claims_text
            )
        generator = SkillGenerator.from_effective_models(
            self.get_effective_models(project.config.preset_name)
        )
        text = generator.generate(
            meta_summary=meta,
            individual_summaries=dict(result.get("individual_summaries") or {}),
            topic=str(result.get("topic") or project.config.topic),
            days=int(result.get("days") or project.config.days),
            provider_results=[
                ProviderResult.from_dict(r)
                for r in result.get("provider_results") or []
            ],
        )
        if text:
            project.save_skill(text)
        return text

    def create_and_run_project(
        self,
        topic: str,
        keywords: list[str] | None = None,
        days: int = 7,
        providers: list[str] | None = None,
        sources: list[str] | None = None,
        include_raw: bool = True,
        preset_name: str | None = None,
        site_research_enabled: bool | None = None,
        site_research_sites: list[str] | None = None,
        progress: ProgressCallback | None = None,
        log_level: str = "DEBUG",
        materials: bool = True,
        materials_limit: int = 0,
        brain: Path | None = None,
        repos_enabled: bool = True,
        repo_keep_clones: bool = False,
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
            repos_enabled=repos_enabled,
            repo_keep_clones=repo_keep_clones,
            materials=materials,
            materials_limit=materials_limit,
            brain=brain,
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
        site_research_enabled: bool | None = None,
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
        providers, site_research_enabled = self.resolve_run_defaults(
            preset_name, providers, site_research_enabled
        )
        # Before the council spends: sub-runs repeat this check per project.
        refuse_api_billed_llm(effective_models, providers)
        if auth_problem := claude_cli_login_problem():
            logger.error(auth_problem, extra={"stage": "claude_cli_auth_error"})

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
                providers=providers,
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
        reason: str | None = None
        try:
            generator = SuperSummaryGenerator.from_effective_models(effective_models)
            super_md = generator.generate_super_summary(improved_topic, days, payloads)
            reason = generator.last_error
        except Exception as e:
            reason = str(e)
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
                "super_summary_error": None if super_md else (reason or "unknown"),
            },
            super_md or _super_summary_unavailable_report(reason),
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
            else:
                self._archive_user_link(project, entry)
            added = entry
        else:
            src_path = Path(stripped).expanduser().resolve()
            added = project.add_user_file_source(src_path, title=title, note=note)

        project.save_config()
        return added

    @staticmethod
    def _archive_user_link(project: Project, entry: UserUrlSource) -> None:
        """Extract the link's text into ``user_sources/`` so the run can use it.

        Best-effort: a link that cannot be read stays registered and citable,
        it just contributes no content. The reason is logged for the caller.
        """
        import re as _re

        from researchkit.user_links import fetch_link

        content = fetch_link(entry.url)
        if not content.ok:
            logger.warning(
                "user link not readable (%s): %s",
                content.error,
                entry.url,
                extra={"stage": "user_link_skip"},
            )
            return
        slug = _re.sub(r"[^a-z0-9]+", "-", entry.url.lower()).strip("-")[:60] or "link"
        index = len(project.config.user_url_sources)
        filename = f"link-{index:03d}-{slug}.md"
        target = project.user_sources_dir
        target.mkdir(parents=True, exist_ok=True)
        header = (
            f"# {entry.title or entry.url}\n\n"
            f"Source: {entry.url}\n"
            f"Kind: {content.kind}\n\n---\n\n"
        )
        (target / filename).write_text(header + content.text, encoding="utf-8")
        entry.filename = filename
        entry.content_kind = content.kind
        logger.info(
            "archived user link (%s via %s): %s",
            content.kind,
            content.via,
            entry.url,
            extra={"stage": "user_link_archived"},
        )

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
