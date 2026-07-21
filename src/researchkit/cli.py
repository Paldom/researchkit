"""
Command-line interface for researchkit.

This is a thin interface layer that delegates to SocialResearchService.

Commands:
    researchkit "topic"              - Instant mode: create project and run
    researchkit create "topic"       - Create project only
    researchkit run <project>        - Run existing project
    researchkit list                 - List all projects
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from researchkit.observability.logging_setup import init_app_logging
from researchkit.project import PROJECTS_DIR, find_project, list_projects
from researchkit.service import SocialResearchService


def _default_projects_dir() -> Path:
    """Projects dir default: $RESEARCHKIT_PROJECTS_DIR, else ./projects.

    The env var lets wrappers (and `uv run --directory` callers, whose cwd is
    the researchkit checkout) pin output somewhere predictable.
    """
    return Path(os.environ.get("RESEARCHKIT_PROJECTS_DIR") or PROJECTS_DIR)


def create_parser() -> argparse.ArgumentParser:
    """Create the argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog="researchkit",
        description="Collect and analyze social insights about a topic using multiple AI providers.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  %(prog)s "AI agents"                    # Instant: create project and run
  %(prog)s create "AI agents"             # Create project only
  %(prog)s run projects/20251229_...      # Run existing project
  %(prog)s list                           # List all projects

Environment variables required:
  OPENAI_API_KEY      - For OpenAI GPT-5.1 and summarization
  GEMINI_API_KEY      - For Google Gemini 3 Pro (or GOOGLE_API_KEY)
  XAI_API_KEY         - For xAI Grok 4.1
  PERPLEXITY_API_KEY  - For Perplexity Sonar Pro
  ZAI_API_KEY         - For z.ai GLM (search provider + generic model)
  TAVILY_API_KEY      - For Tavily search (optional)
  ANTHROPIC_API_KEY   - For Claude Code with web search (optional)
  GITHUB_TOKEN        - For GitHub search (optional, higher rate limits)

Site Research (optional):
""",
    )

    # Global options
    parser.add_argument(
        "--projects-dir",
        type=Path,
        default=_default_projects_dir(),
        help=(
            f"Directory for projects (default: $RESEARCHKIT_PROJECTS_DIR "
            f"or {PROJECTS_DIR})"
        ),
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show detailed progress information",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress console log output (logs still written to file)",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Log level (default: INFO)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # --- create command ---
    create_parser = subparsers.add_parser(
        "create",
        help="Create a new research project (without running)",
    )
    _add_topic_args(create_parser)
    _add_research_args(create_parser)

    # --- run command ---
    run_parser = subparsers.add_parser(
        "run",
        help="Run research on an existing project",
    )
    run_parser.add_argument(
        "project",
        help="Project folder name or path",
    )
    run_parser.add_argument(
        "--materials",
        action="store_true",
        help="After the run, download cited sources into materials/",
    )
    run_parser.add_argument(
        "--materials-limit",
        type=int,
        default=25,
        help="Max sources to fetch with --materials, 0 = all (default: 25)",
    )
    run_parser.add_argument(
        "--ingest",
        metavar="BRAIN_DIR",
        default=None,
        help=(
            "After the run, ingest the project into a brainkit brain at this "
            "path (requires brainkit installed in the same environment)"
        ),
    )

    # --- advise command ---
    advise_parser = subparsers.add_parser(
        "advise",
        help=(
            "Ask each subscription CLI harness (Claude Code, Codex, Antigravity, "
            "Grok CLI, Kimi Code CLI) the same question and print every answer — "
            "no API keys"
        ),
    )
    advise_parser.add_argument("question", help="The question to ask every harness")
    advise_parser.add_argument(
        "--context-file",
        default=None,
        help="File whose text is appended to the question as context",
    )
    advise_parser.add_argument(
        "--harnesses",
        nargs="+",
        default=None,
        help=(
            "Member specs override, e.g. claude:claude-opus-4-8@xhigh codex:gpt-5.6-sol "
            "(default: the harness preset's council members)"
        ),
    )
    advise_parser.add_argument(
        "--preset",
        default="harness",
        help="Preset supplying the members (default: harness)",
    )

    # --- doctor command ---
    doctor_parser = subparsers.add_parser(
        "doctor",
        help=(
            "Preflight the active preset: CLI harnesses installed/logged in, "
            "pinned model ids still exist, API keys present — no tokens spent"
        ),
    )
    doctor_parser.add_argument(
        "--preset",
        default=None,
        help="Preset to check (default: the active preset)",
    )

    # --- council command ---
    council_parser = subparsers.add_parser(
        "council",
        help=(
            "Convene the harness council on a question: members answer through "
            "distinct lenses, a boss synthesizes one answer — no API keys"
        ),
    )
    council_parser.add_argument("question", help="The question to deliberate on")
    council_parser.add_argument(
        "--context-file",
        default=None,
        help="File whose text is appended to the question as context",
    )
    council_parser.add_argument(
        "--harnesses",
        nargs="+",
        default=None,
        help="Member specs override (default: the harness preset's council members)",
    )
    council_parser.add_argument(
        "--boss", default=None, help="Boss spec override (default: preset boss)"
    )
    council_parser.add_argument(
        "--preset",
        default="harness",
        help="Preset supplying members and boss (default: harness)",
    )

    # --- explore command ---
    explore_parser = subparsers.add_parser(
        "explore",
        help=(
            "Boosted research on subscription harnesses only (no API keys): "
            "council refines the topic, CLI providers research in parallel"
        ),
    )
    explore_parser.add_argument("topic", help="The topic to research")
    explore_parser.add_argument(
        "--days", "-d", type=int, default=7, help="Lookback window (default: 7)"
    )
    explore_parser.add_argument(
        "--materials",
        action="store_true",
        help="Download cited sources into materials/ after the run",
    )
    explore_parser.add_argument(
        "--materials-limit",
        type=int,
        default=25,
        help="Max sources to fetch with --materials, 0 = all (default: 25)",
    )
    explore_parser.add_argument(
        "--ingest",
        metavar="BRAIN_DIR",
        default=None,
        help="Ingest the finished project into a brainkit brain at this path",
    )
    explore_parser.add_argument(
        "--no-raw", action="store_true", help="Exclude raw provider outputs"
    )
    explore_parser.add_argument(
        "--preset",
        default="harness",
        help="Preset to run with (default: harness — subscription CLIs only)",
    )

    # --- plugins command ---
    subparsers.add_parser(
        "plugins",
        help="List installed research plugins and their activation status",
    )

    # --- materials command ---
    materials_parser = subparsers.add_parser(
        "materials",
        help="Download the sources cited by a completed run into materials/",
    )
    materials_parser.add_argument("project", help="Project folder name or path")
    materials_parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Max sources to fetch, 0 = all (default: 25)",
    )
    materials_parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-fetch sources whose material file already exists",
    )

    # --- list command ---
    subparsers.add_parser(
        "list",
        help="List all projects",
    )

    # --- links command ---
    links_parser = subparsers.add_parser(
        "links",
        help="Analyze citation links for a project",
    )
    links_parser.add_argument(
        "project",
        help="Project folder name or path",
    )
    links_parser.add_argument(
        "--mode",
        choices=["strict", "loose"],
        default="loose",
        help="URL normalization mode (default: loose)",
    )
    links_parser.add_argument(
        "--top-domains",
        type=int,
        default=20,
        help="Number of top domains to show (default: 20)",
    )
    links_parser.add_argument(
        "--top-duplicates",
        type=int,
        default=20,
        help="Number of top duplicate groups to show (default: 20)",
    )
    links_parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON instead of formatted text",
    )
    links_parser.add_argument(
        "--save",
        action="store_true",
        help="Save link_analytics.json to the project folder",
    )

    # --- add-source command ---
    add_src_parser = subparsers.add_parser(
        "add-source",
        help="Add a user-curated source (URL or local file) to a project",
        description=(
            "Add a URL or local file as a user-curated source. URLs are cited in "
            "the final article. Files are copied into the project and used as "
            "additional context for the final article only (the file itself is "
            "not cited; URLs/book titles inside it may be)."
        ),
    )
    add_src_parser.add_argument("project", help="Project folder name or path")
    add_src_parser.add_argument(
        "source",
        help="A URL (http:// or https://) or a path to a local file",
    )
    add_src_parser.add_argument("--title", default=None, help="Optional title")
    add_src_parser.add_argument("--note", default=None, help="Optional note")

    # --- remove-source command ---
    rm_src_parser = subparsers.add_parser(
        "remove-source",
        help="Remove a user-curated source from a project",
    )
    rm_src_parser.add_argument("project", help="Project folder name or path")
    rm_src_parser.add_argument(
        "identifier",
        help=(
            "URL, filename, or 1-based index from `list-sources` (URLs first, "
            "then files)"
        ),
    )

    # --- list-sources command ---
    ls_src_parser = subparsers.add_parser(
        "list-sources",
        help="List user-curated sources for a project",
    )
    ls_src_parser.add_argument("project", help="Project folder name or path")

    # --- suggest-prompt command ---
    sp_parser = subparsers.add_parser(
        "suggest-prompt",
        help=(
            "Print a starter prompt for Claude Code that loads the full "
            "project context to iterate on the article"
        ),
    )
    sp_parser.add_argument("project", help="Project folder name or path")

    # --- improve-topic command ---
    improve_parser = subparsers.add_parser(
        "improve-topic",
        help="Improve topic text for better research results",
    )
    improve_parser.add_argument(
        "topic",
        help="Topic text to improve",
    )
    improve_parser.add_argument(
        "--provider",
        default="openai",
        help="Provider to use (default: uses system config improver model)",
    )
    improve_parser.add_argument(
        "--model",
        help="Model override (default: uses system config improver model)",
    )
    improve_parser.add_argument(
        "--no-council",
        action="store_true",
        help="Use the single-model improver instead of the LLM council",
    )

    # --- generate-keywords command ---
    kw_parser = subparsers.add_parser(
        "generate-keywords",
        help="Generate search keywords for a topic",
    )
    kw_parser.add_argument(
        "topic",
        help="Topic to generate keywords for",
    )
    kw_parser.add_argument(
        "--count",
        "-n",
        type=int,
        default=10,
        help="Number of keywords to generate (default: 10)",
    )
    kw_parser.add_argument(
        "--provider",
        default="openai",
        help="Provider to use (default: uses system config improver model)",
    )
    kw_parser.add_argument(
        "--model",
        help="Model override (default: uses system config improver model)",
    )
    kw_parser.add_argument(
        "--no-council",
        action="store_true",
        help="Use the single-model improver instead of the LLM council",
    )

    # --- Default: instant mode (topic as positional) ---
    # This is handled specially - if no subcommand, treat first positional as topic

    return parser


def _add_topic_args(parser: argparse.ArgumentParser) -> None:
    """Add topic-related arguments."""
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "topic",
        nargs="?",
        default=None,
        help="The topic to research (e.g., 'AI safety', 'remote work trends')",
    )
    group.add_argument(
        "--from-file",
        "-F",
        action="store_true",
        help="Read topic from PROMPT.md file instead of command line argument",
    )


def _add_research_args(parser: argparse.ArgumentParser) -> None:
    """Add research configuration arguments."""
    parser.add_argument(
        "--days",
        "-d",
        type=int,
        default=7,
        help="Number of days to look back (default: 7)",
    )
    parser.add_argument(
        "--keywords",
        "-k",
        nargs="+",
        default=[],
        help="Search keywords to guide research (optional)",
    )
    parser.add_argument(
        "--materials",
        action="store_true",
        help="After the run, download cited sources into materials/ (ignored by create)",
    )
    parser.add_argument(
        "--materials-limit",
        type=int,
        default=25,
        help="Max sources to fetch with --materials, 0 = all (default: 25)",
    )
    parser.add_argument(
        "--ingest",
        metavar="BRAIN_DIR",
        default=None,
        help=(
            "After the run, ingest the project into a brainkit brain at this "
            "path (requires brainkit installed in the same environment; "
            "ignored by create)"
        ),
    )
    parser.add_argument(
        "--providers",
        "-p",
        nargs="+",
        default=["openai", "gemini", "grok", "perplexity"],
        help=(
            "Providers to query (default: openai gemini grok perplexity; "
            "see `researchkit plugins` for everything available)"
        ),
    )
    parser.add_argument(
        "--sources",
        nargs="+",
        default=["social", "web"],
        choices=["social", "web"],
        help="Sources to query: social, web, or both (default: social web)",
    )
    parser.add_argument(
        "--no-raw",
        action="store_true",
        help="Exclude raw provider outputs from report",
    )
    parser.add_argument(
        "--no-site-research",
        action="store_true",
        help="Disable keyword-based site research (Exa)",
    )
    parser.add_argument(
        "--site-research-sites",
        nargs="+",
        default=None,
        help=(
            "Sites to search for site research (default: every active "
            "connector — see `researchkit plugins`)"
        ),
    )
    parser.add_argument(
        "--preset",
        default=None,
        help=(
            "Model preset from models.yaml for this run (default: the active "
            "preset; e.g. --preset hybrid)"
        ),
    )
    parser.add_argument(
        "--boost",
        action="store_true",
        help=(
            "Boost mode: convene the LLM council to refine the topic and, if it "
            "judges the topic worth decomposing, fan out into parallel sub-projects "
            "with an opus-authored super-summary on top"
        ),
    )


def make_progress_callback(verbose: bool):
    """Create a progress callback for CLI output."""

    def progress(evt: dict) -> None:
        if not verbose:
            return
        stage = evt.get("stage", "")
        message = evt.get("message", "")

        if stage == "start":
            print(f"\n[start] {message}", file=sys.stderr)
            print(f"  Run ID: {evt.get('run_id', 'unknown')}", file=sys.stderr)
        elif stage == "collecting":
            print(f"[collecting] {message}", file=sys.stderr)
        elif stage == "provider_start":
            print(f"  [provider] Starting {evt.get('provider')}...", file=sys.stderr)
        elif stage == "provider_done":
            provider = evt.get("provider", "unknown")
            ok = evt.get("ok", False)
            done = evt.get("done", 0)
            total = evt.get("total", 0)
            sources = evt.get("sources", 0)
            status = "OK" if ok else "FAILED"
            print(
                f"  [provider] {provider}: {status} ({sources} sources) [{done}/{total}]",
                file=sys.stderr,
            )
        elif stage == "summarizing":
            print(f"[summarizing] {message}", file=sys.stderr)
        elif stage == "meta_summarizing":
            print(f"[meta_summarizing] {message}", file=sys.stderr)
        elif stage == "keyword_synthesis_start":
            print(f"[keyword_synthesis] {message}", file=sys.stderr)
        elif stage == "keyword_synthesis_done":
            count = evt.get("count", 0)
            print(f"[keyword_synthesis] Done: {count} keywords", file=sys.stderr)
        elif stage == "site_research_start":
            print(f"[site_research] {message}", file=sys.stderr)
        elif stage == "site_research_done":
            total = evt.get("total_items", 0)
            errors = evt.get("errors", 0)
            print(
                f"[site_research] Done: {total} items, {errors} errors", file=sys.stderr
            )
        elif stage == "digest" or stage == "digest_done":
            print(f"[digest] {message}", file=sys.stderr)
        elif stage == "formatting":
            print(f"[formatting] {message}", file=sys.stderr)
        elif stage == "saved":
            print(f"[saved] {message}", file=sys.stderr)
        elif stage == "done":
            print(f"[done] {message}\n", file=sys.stderr)

    return progress


def resolve_topic(args) -> str | None:
    """Resolve topic from args or PROMPT.md file."""
    if getattr(args, "from_file", False):
        prompt_file = Path("PROMPT.md")
        if not prompt_file.exists():
            print("Error: PROMPT.md file not found", file=sys.stderr)
            return None
        topic = prompt_file.read_text(encoding="utf-8").strip()
        if not topic:
            print("Error: PROMPT.md is empty", file=sys.stderr)
            return None
        return topic
    return getattr(args, "topic", None)


def cmd_create(args, service: SocialResearchService) -> int:
    """Handle the 'create' command."""
    topic = resolve_topic(args)
    if not topic:
        return 1

    project = service.create_project(
        topic=topic,
        keywords=getattr(args, "keywords", []),
        days=args.days,
        providers=list(args.providers),
        sources=list(args.sources),
        include_raw=not args.no_raw,
        preset_name=getattr(args, "preset", None),
        site_research_enabled=not getattr(args, "no_site_research", False),
        site_research_sites=getattr(args, "site_research_sites", None),
    )

    print(f"Created project: {project.path}", file=sys.stderr)
    print(f"Config saved to: {project.config_path}", file=sys.stderr)
    print("\nTo run this project:", file=sys.stderr)
    print(f"  researchkit run {project.path}", file=sys.stderr)

    return 0


def _download_materials_for(project, limit: int = 25, refresh: bool = False) -> int:
    """Shared materials-download step for run/instant/boost/materials commands."""
    from researchkit.materials import download_materials

    try:
        manifest = download_materials(project, limit=limit, refresh=refresh)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    counts: dict[str, int] = {}
    for entry in manifest["entries"]:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
    hint = " (--materials-limit 0 to fetch all)" if counts.get("skipped_limit") else ""
    print(
        f"Materials: {manifest['fetched']}/{manifest['total_cited']} sources "
        f"archived in {(project.path / 'materials').resolve()} ({summary}){hint}",
        file=sys.stderr,
    )
    return 0


def _ingest_into_brain(project, brain_dir: str) -> int:
    """One-shot research→brain handoff: ingest the finished project.

    brainkit is a soft dependency — only needed when --ingest is passed.
    A boosted parent's subprojects/ are ingested recursively by brainkit.
    """
    try:
        from brainkit.brain import ingest_research_project
    except ImportError:
        print(
            "Error: --ingest requires brainkit in this environment "
            "(e.g. `uv pip install -e ../brainkit`). The run itself succeeded; "
            f'ingest manually: brainkit ingest "{project.path.resolve()}"',
            file=sys.stderr,
        )
        return 1
    try:
        report = ingest_research_project(
            project.path.resolve(), Path(brain_dir).expanduser()
        )
    except Exception as e:
        # Never let a brain problem (read-only dir, OSError, …) crash the CLI
        # after a completed paid run — the report must still reach stdout.
        print(f"Error: ingest failed: {e}", file=sys.stderr)
        return 1
    # getattr: tolerate an older installed brainkit without sub-report support
    subs = getattr(report, "sub_reports", [])
    total_sources = len(report.source_notes) + sum(len(r.source_notes) for r in subs)
    print(
        f"Ingested into brain {Path(brain_dir).expanduser().resolve()}: "
        f"{1 + len(subs)} topic notes, {total_sources} source notes",
        file=sys.stderr,
    )
    return 0


def _validate_registry_choices(
    values: list[str], valid: list[str], flag: str
) -> str | None:
    """Human error message when values aren't registered, else None."""
    unknown = [v for v in values if v not in valid]
    if unknown:
        return (
            f"Unknown {flag}: {', '.join(unknown)}. "
            f"Available: {', '.join(valid)} (see `researchkit plugins`)."
        )
    return None


def cmd_plugins(args) -> int:
    """Handle the 'plugins' command: activation status + provenance."""
    from researchkit import plugins as plugin_mod

    registry = plugin_mod.get_registry(refresh=True)
    print("Built-in providers:", ", ".join(sorted(plugin_mod.get_registry().providers)))
    builtin_connectors = [
        n
        for n in registry.connectors
        if all(n not in p.connectors for p in registry.plugins)
    ]
    print("Built-in connectors:", ", ".join(sorted(builtin_connectors)))
    if not registry.plugins:
        print("\nNo plugins installed. Install one and set its API key —")
        print("see the README's plugin guide.")
        return 0
    print("\nPlugins:")
    for rec in registry.plugins:
        extensions = ", ".join([*rec.providers, *rec.connectors]) or "-"
        line = f"  {rec.dist} {rec.version}  [{rec.status}]"
        if rec.reason:
            line += f"  ({rec.reason})"
        print(line)
        print(f"      extensions: {extensions}")
        if rec.origin:
            print(f"      origin: {rec.origin}")
    return 0


def cmd_materials(args, service: SocialResearchService) -> int:
    """Handle the 'materials' command."""
    project = find_project(args.project, args.projects_dir)
    if not project:
        print(f"Error: Project not found: {args.project}", file=sys.stderr)
        return 1
    return _download_materials_for(project, limit=args.limit, refresh=args.refresh)


def cmd_run(args, service: SocialResearchService) -> int:
    """Handle the 'run' command."""
    project = find_project(args.project, args.projects_dir)
    if not project:
        print(f"Error: Project not found: {args.project}", file=sys.stderr)
        return 1

    if project.has_results:
        print("Warning: Project already has results, overwriting...", file=sys.stderr)

    print(f"Running project: {project.path}", file=sys.stderr)
    print(f"Topic: {project.config.topic}", file=sys.stderr)

    progress = make_progress_callback(args.verbose)

    try:
        artifacts = service.run_project(
            project,
            progress=progress,
            log_level=args.log_level,
        )
    except KeyboardInterrupt:
        print("\nInterrupted by user", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    failures = [r for r in artifacts.bundle.provider_results if not r.is_success]
    all_failed = bool(failures) and len(failures) == len(
        artifacts.bundle.provider_results
    )

    # No materials/ingest for a run with zero successful providers — there is
    # nothing cited to archive, and a hollow topic note would pollute a brain.
    handoff_rc = 0
    if not all_failed:
        if getattr(args, "materials", False):
            _download_materials_for(project, limit=getattr(args, "materials_limit", 25))
        if getattr(args, "ingest", None):
            handoff_rc = _ingest_into_brain(project, args.ingest)

    # Output report
    print(artifacts.report_markdown)
    print(f"wrote: {project.path.resolve()}", file=sys.stderr)
    _print_next_steps(project.path.resolve())

    # Show run metadata
    if args.verbose:
        print("\n--- Run Metadata ---", file=sys.stderr)
        print(f"Run ID: {artifacts.run_id}", file=sys.stderr)
        print(f"Report: {artifacts.report_path}", file=sys.stderr)
        if artifacts.log_path:
            print(f"Log: {artifacts.log_path}", file=sys.stderr)

    if all_failed:
        return 1

    # Suggest a Claude Code prompt to iterate on the article.
    _print_article_prompt_hint(project)

    if failures and args.verbose:
        print(f"\nWarning: {len(failures)} provider(s) failed", file=sys.stderr)
    # A requested --ingest that failed is a pipeline failure even though the
    # research succeeded (agent callers must not proceed to an empty brain).
    return handoff_rc


def cmd_list(args, service: SocialResearchService) -> int:
    """Handle the 'list' command."""
    projects = list_projects(args.projects_dir)

    if not projects:
        print(f"No projects found in {args.projects_dir}", file=sys.stderr)
        return 0

    print(f"Projects in {args.projects_dir}:\n")
    for p in projects:
        status = "completed" if p.has_results else "pending"
        created = p.created_at.strftime("%Y-%m-%d %H:%M")
        print(f"  [{status:9}] {p.name}")
        print(f"             Topic: {p.config.topic}")
        print(f"             Created: {created}")
        print()

    return 0


def cmd_links(args, service: SocialResearchService) -> int:
    """Handle the 'links' command - analyze citation links."""
    import json

    from researchkit.link_analytics import (
        analyze_occurrences,
        occurrences_from_result_json,
    )

    project = find_project(args.project, args.projects_dir)
    if not project:
        print(f"Error: Project not found: {args.project}", file=sys.stderr)
        return 1

    # Analyze citations from result.json
    results = project.load_results()
    if not results:
        print("Error: result.json not found (project not run yet?)", file=sys.stderr)
        return 1

    occ = occurrences_from_result_json(results)
    if not occ:
        print("Error: No links found in result.json", file=sys.stderr)
        return 1

    data = analyze_occurrences(
        occ,
        dataset_label="citations",
        mode=args.mode,
        top_n_domains=args.top_domains,
        top_n_duplicates=args.top_duplicates,
    ).to_dict()

    # Output
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        summary = data.get("summary", {})
        print(f"\n{'=' * 60}")
        print(f"[CITATIONS] mode={data.get('mode', 'loose')}")
        print(f"{'=' * 60}")
        print(f"  Total occurrences: {summary.get('total_occurrences', 0)}")
        print(f"  Unique URLs:       {summary.get('unique_urls', 0)}")
        print(
            f"  Duplicates:        {summary.get('duplicate_occurrences', 0)} ({summary.get('duplicate_rate', 0):.1%})"
        )
        print(f"  Unique domains:    {summary.get('unique_domains', 0)}")

        # Provider breakdown
        by_provider = data.get("counts_by_provider", {})
        if by_provider:
            print("\n  Links by provider:")
            for provider, count in sorted(
                by_provider.items(), key=lambda x: x[1], reverse=True
            ):
                print(f"    - {provider}: {count}")

        # Source type breakdown
        by_source = data.get("counts_by_source_type", {})
        if by_source:
            print("\n  Links by source type:")
            for stype, count in sorted(
                by_source.items(), key=lambda x: x[1], reverse=True
            ):
                print(f"    - {stype}: {count}")

        # Top domains
        top_domains = data.get("top_domains", [])
        if top_domains:
            print(f"\n  Top {min(10, len(top_domains))} domains:")
            for domain, count in top_domains[:10]:
                domain_display = domain or "(unknown)"
                print(f"    - {domain_display}: {count}")

        # Top duplicates
        top_dups = data.get("top_duplicates", [])
        if top_dups:
            print(f"\n  Top {min(5, len(top_dups))} duplicate groups:")
            for dup in top_dups[:5]:
                providers_str = ", ".join(dup.get("providers", []))
                print(
                    f"    - {dup.get('occurrences', 0)}x: {dup.get('canonical_url', '')[:60]}..."
                )
                print(f"      Providers: {providers_str}")

    # Save if requested
    if args.save:
        out_path = project.path / "link_analytics.json"
        out_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"\nSaved: {out_path}", file=sys.stderr)

    return 0


def cmd_add_source(args, service: SocialResearchService) -> int:
    """Handle the 'add-source' command."""
    project = find_project(args.project, args.projects_dir)
    if not project:
        print(f"Error: Project not found: {args.project}", file=sys.stderr)
        return 1

    urls_before = len(project.config.user_url_sources)
    try:
        added = service.add_user_source(
            project,
            args.source,
            title=args.title,
            note=args.note,
        )
    except (ValueError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    from researchkit.project import UserFileSource, UserUrlSource

    if isinstance(added, UserUrlSource):
        # A duplicate URL is silently ignored by the project; report accurately
        # instead of always claiming "Added". (Review cli.py:597.)
        if len(project.config.user_url_sources) > urls_before:
            print(f"Added URL source: {added.url}", file=sys.stderr)
        else:
            print(
                f"URL source already present (not added): {added.url}", file=sys.stderr
            )
    elif isinstance(added, UserFileSource):
        copied = project.user_sources_dir / added.filename
        print(f"Added file source: {copied}", file=sys.stderr)
    return 0


def cmd_remove_source(args, service: SocialResearchService) -> int:
    """Handle the 'remove-source' command."""
    project = find_project(args.project, args.projects_dir)
    if not project:
        print(f"Error: Project not found: {args.project}", file=sys.stderr)
        return 1

    if service.remove_user_source(project, args.identifier):
        print(f"Removed: {args.identifier}", file=sys.stderr)
        return 0
    print(f"No matching source for: {args.identifier}", file=sys.stderr)
    return 1


def cmd_list_sources(args, service: SocialResearchService) -> int:
    """Handle the 'list-sources' command."""
    project = find_project(args.project, args.projects_dir)
    if not project:
        print(f"Error: Project not found: {args.project}", file=sys.stderr)
        return 1

    urls, files = service.list_user_sources(project)
    if not urls and not files:
        print("(no user-curated sources)")
        return 0

    idx = 1
    if urls:
        print("URLs (cited in final article):")
        for u in urls:
            label = f"  {idx}. {u.url}"
            if u.title:
                label += f"  — title: {u.title}"
            if u.note:
                label += f"  — note: {u.note}"
            print(label)
            idx += 1

    if files:
        print("Files (context only, not cited):")
        for f in files:
            label = f"  {idx}. {f.filename}"
            if f.title:
                label += f"  — title: {f.title}"
            if f.note:
                label += f"  — note: {f.note}"
            print(label)
            idx += 1

    return 0


def cmd_suggest_prompt(args, service: SocialResearchService) -> int:
    """Handle the 'suggest-prompt' command - emit Claude Code starter prompt."""
    from researchkit.article_prompt import build_article_prompt

    project = find_project(args.project, args.projects_dir)
    if not project:
        print(f"Error: Project not found: {args.project}", file=sys.stderr)
        return 1

    print(build_article_prompt(project))
    return 0


def _print_article_prompt_hint(project) -> None:
    """Print the Claude Code starter prompt to stderr after a successful run."""
    from researchkit.article_prompt import build_article_prompt

    prompt = build_article_prompt(project)
    print(
        "\n--- Claude Code starter prompt (copy/paste to iterate on the article) ---",
        file=sys.stderr,
    )
    print(prompt, file=sys.stderr)
    print("--- end prompt ---\n", file=sys.stderr)


def _harness_council(preset: str, harnesses=None, boss=None):
    """Build an LLMCouncil from a preset, with optional member/boss overrides."""
    from researchkit.council import LLMCouncil
    from researchkit.system_config import SystemConfigManager

    council = LLMCouncil.from_effective_models(
        SystemConfigManager().resolve_effective_models(preset)
    )
    if harnesses:
        council.members = list(harnesses)
    if boss:
        council.boss = boss
    return council


def _question_with_context(args) -> str:
    """The question, with --context-file text appended when given."""
    question = args.question
    context_file = getattr(args, "context_file", None)
    if context_file:
        text = Path(context_file).read_text(encoding="utf-8").strip()
        question = f"{question}\n\nCONTEXT:\n{text}"
    return question


def _print_next_steps(path: Path) -> None:
    """The trio fork, printed as runnable commands (the journey's next leg
    is otherwise tribal knowledge — reviewer consensus, 2026-07-21)."""
    import sys as _sys

    p = str(path)
    print(
        "next steps:\n"
        f"  compounding memory : brainkit --brain ./brain ingest {p!r}\n"
        f"  one-shot skill     : /skill-from-research {p!r}  (in an agent session)\n"
        "  pack contract      : docs/research-pack.md",
        file=_sys.stderr,
    )


def cmd_doctor(args) -> int:
    """Handle 'doctor': preflight the preset's slots, harnesses, and keys."""
    from researchkit.doctor import format_report, run_doctor
    from researchkit.system_config import SystemConfigManager

    preset = args.preset or SystemConfigManager().get_active_preset()
    try:
        results = run_doctor(args.preset)
    except ValueError as e:  # unknown preset
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(format_report(results, preset))
    return 1 if any(r.status == "fail" for r in results) else 0


def cmd_advise(args) -> int:
    """Handle 'advise': one question, every harness's own answer, verbatim."""
    try:
        council = _harness_council(args.preset, args.harnesses)
        question = _question_with_context(args)
    except (ValueError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print(
        f"Asking {len(council.members)} harnesses: {', '.join(council.members)} …",
        file=sys.stderr,
    )
    answers = council.advise(question)
    for a in answers:
        print(f"\n## {a.member}\n")
        print(a.answer if a.ok else f"*failed: {a.error}*")
    ok = sum(1 for a in answers if a.ok)
    print(f"\n{ok}/{len(answers)} harnesses answered.", file=sys.stderr)
    return 0 if ok else 1


def cmd_council(args) -> int:
    """Handle 'council': lensed member answers + boss-synthesized consultation."""
    try:
        council = _harness_council(args.preset, args.harnesses, args.boss)
        question = _question_with_context(args)
    except (ValueError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print(
        f"Convening council ({', '.join(council.members)}; boss={council.boss}) …",
        file=sys.stderr,
    )
    try:
        result = council.consult(question)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if not result.boss_synthesized:
        print(
            "Warning: boss synthesis unavailable; showing the first valid "
            "member answer.",
            file=sys.stderr,
        )
    header = (
        f"confidence: {result.confidence or 'unstated'}, "
        f"convergence: {result.convergence or 'unknown'}"
    )
    print(f"\n# Council answer ({header})\n")
    print(result.answer)
    if result.dissent:
        print(f"\n## Dissent\n\n{result.dissent}")
    print("\n## Members\n")
    for a in result.answers:
        if a.ok:
            line = f"- **{a.member}** ({a.lens}, confidence: {a.confidence or '-'})"
            if a.rationale:
                line += f" — {a.rationale}"
        else:
            line = f"- **{a.member}** — failed: {a.error}"
        print(line)
    if args.verbose:
        print("\n## Full member answers\n")
        for a in result.answers:
            if a.ok:
                print(f"### {a.member} ({a.lens})\n\n{a.answer}\n")
    return 0


def cmd_explore(args, service: SocialResearchService) -> int:
    """Handle 'explore': boosted research on the subscription-only preset.

    Providers are the five CLI harnesses; site research is off (its keyword
    synthesis and Exa connector are API-key paths). Everything else is the
    normal boost pipeline.
    """
    args.boost = True
    args.providers = ["openai", "gemini", "grok", "claude", "kimi"]
    args.sources = ["social", "web"]
    args.no_site_research = True
    args.preset_name = args.preset
    return _cmd_instant_boosted(args, service, args.topic)


def _make_topic_helper(args):
    """Return the topic/keyword helper: the LLM council by default, or a single
    PromptImprover when the user passes an explicit --provider/--model override
    or --no-council. Both expose improve_topic() and generate_keywords()."""
    explicit_override = args.provider != "openai" or args.model is not None
    if getattr(args, "no_council", False) or explicit_override:
        from researchkit.prompt_improver import PromptImprover

        if not explicit_override:
            return PromptImprover.from_system_config()
        return PromptImprover(provider=args.provider, model=args.model)
    from researchkit.council import LLMCouncil

    return LLMCouncil.from_system_config()


def cmd_improve_topic(args) -> int:
    """Handle the 'improve-topic' command."""
    try:
        helper = _make_topic_helper(args)
        print(helper.improve_topic(args.topic))
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_generate_keywords(args) -> int:
    """Handle the 'generate-keywords' command."""
    try:
        helper = _make_topic_helper(args)
        for kw in helper.generate_keywords(args.topic, count=args.count):
            print(kw)
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def _make_boost_progress(args):
    """Progress callback for boost runs: verbose behavior plus a heartbeat.

    Boost runs take many minutes; without this a non-verbose caller sees no
    output between "Convening council" and the report and can't tell waiting
    from hung. Compact [boost] lines go to stderr even without --verbose.
    """
    verbose_cb = make_progress_callback(args.verbose)
    heartbeat_stages = {
        "council_start",
        "council_done",
        "boost_collecting",
        "super_summary_start",
        "super_summary_done",
    }

    def progress(evt: dict) -> None:
        verbose_cb(evt)
        stage = evt.get("stage", "")
        # Boost lifecycle stages are unknown to make_progress_callback, so
        # print them in verbose mode too.
        if stage in heartbeat_stages:
            print(f"[boost] {evt.get('message', stage)}", file=sys.stderr)
            return
        if args.verbose:
            return  # per-event detail already printed by verbose_cb
        # An event without "subproject" is the parent run itself (the council
        # declined to decompose) — it deserves the same liveness signal.
        sub = evt.get("subproject", "")
        label = f"{sub[:60]}: " if sub else ""
        if stage == "provider_done":
            status = "OK" if evt.get("ok") else "FAILED"
            print(
                f"[boost] {label}{evt.get('provider', '?')} {status} "
                f"({evt.get('done', 0)}/{evt.get('total', 0)} providers)",
                file=sys.stderr,
            )
        elif stage == "done":
            print(f"[boost] {label}{evt.get('message', 'done')}", file=sys.stderr)

    return progress


def _cmd_instant_boosted(args, service: SocialResearchService, topic: str) -> int:
    """Handle instant mode with the LLM council + optional boost fan-out."""
    print(f"Convening council for: {topic}", file=sys.stderr)
    progress = _make_boost_progress(args)

    try:
        result = service.create_and_run_boosted(
            topic=topic,
            days=getattr(args, "days", 7),
            preset_name=getattr(args, "preset_name", None)
            or getattr(args, "preset", None),
            providers=getattr(args, "providers", None),
            sources=getattr(args, "sources", None),
            include_raw=not getattr(args, "no_raw", False),
            site_research_enabled=not getattr(args, "no_site_research", False),
            site_research_sites=getattr(args, "site_research_sites", None),
            force_boost=True,
            progress=progress,
            log_level=args.log_level,
        )
    except KeyboardInterrupt:
        print("\nInterrupted by user", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    cr = result.council_result
    print(f"\nImproved topic: {cr.improved_topic}", file=sys.stderr)

    if not result.decomposed:
        print(
            "Council did not decompose; ran a single improved project.", file=sys.stderr
        )
        artifacts = result.single_artifacts
        handoff_rc = 0
        if artifacts:
            if getattr(args, "materials", False):
                _download_materials_for(
                    result.project, limit=getattr(args, "materials_limit", 25)
                )
            if getattr(args, "ingest", None):
                handoff_rc = _ingest_into_brain(result.project, args.ingest)
            print(artifacts.report_markdown)
            print(f"wrote: {result.project.path.resolve()}", file=sys.stderr)
            _print_next_steps(result.project.path.resolve())
            if args.verbose:
                print(f"\nProject: {result.project.path}", file=sys.stderr)
            _print_article_prompt_hint(result.project)
        return handoff_rc

    print(
        f"Decomposed into {len(result.sub_projects)} sub-projects "
        f"({len(result.sub_artifacts)} completed):",
        file=sys.stderr,
    )
    for sp in result.sub_projects:
        print(f"  - {sp.config.topic}  [{sp.path}]", file=sys.stderr)

    # Each completed sub-project cites real URLs — archive them there. The
    # parent's report cites the sub-reports, not the web, so it has no
    # materials of its own (brainkit ingests subprojects/ recursively).
    handoff_rc = 0
    if getattr(args, "materials", False):
        for sp in result.sub_projects:
            if sp.has_results:
                _download_materials_for(sp, limit=getattr(args, "materials_limit", 25))
    if getattr(args, "ingest", None):
        handoff_rc = _ingest_into_brain(result.project, args.ingest)

    if result.super_summary_markdown:
        print(result.super_summary_markdown)
    else:
        print("Warning: super-summary unavailable.", file=sys.stderr)

    print(f"wrote: {result.project.path.resolve()}", file=sys.stderr)
    _print_next_steps(result.project.path.resolve())
    print(f"Parent project: {result.project.path}", file=sys.stderr)
    print(f"Super-summary: {result.project.super_summary_path}", file=sys.stderr)
    return handoff_rc


def cmd_instant(args, service: SocialResearchService, topic: str) -> int:
    """Handle instant mode (create + run)."""
    # Guard against an empty/whitespace topic launching a full paid run. (Review L28.)
    if not topic or not topic.strip():
        print("Error: topic must not be empty", file=sys.stderr)
        return 1

    if getattr(args, "boost", False):
        return _cmd_instant_boosted(args, service, topic)

    print(f"Creating and running project for: {topic}", file=sys.stderr)

    progress = make_progress_callback(args.verbose)

    try:
        project, artifacts = service.create_and_run_project(
            topic=topic,
            keywords=getattr(args, "keywords", []),
            days=getattr(args, "days", 7),
            providers=getattr(args, "providers", None),
            sources=getattr(args, "sources", None),
            include_raw=not getattr(args, "no_raw", False),
            preset_name=getattr(args, "preset", None),
            site_research_enabled=not getattr(args, "no_site_research", False),
            site_research_sites=getattr(args, "site_research_sites", None),
            progress=progress,
            log_level=args.log_level,
        )
    except KeyboardInterrupt:
        print("\nInterrupted by user", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    failures = [r for r in artifacts.bundle.provider_results if not r.is_success]
    all_failed = bool(failures) and len(failures) == len(
        artifacts.bundle.provider_results
    )

    # No materials/ingest for a run with zero successful providers — there is
    # nothing cited to archive, and a hollow topic note would pollute a brain.
    handoff_rc = 0
    if not all_failed:
        if getattr(args, "materials", False):
            _download_materials_for(project, limit=getattr(args, "materials_limit", 25))
        if getattr(args, "ingest", None):
            handoff_rc = _ingest_into_brain(project, args.ingest)

    # Output report
    print(artifacts.report_markdown)
    print(f"wrote: {project.path.resolve()}", file=sys.stderr)
    _print_next_steps(project.path.resolve())

    # Show run metadata
    if args.verbose:
        print("\n--- Run Metadata ---", file=sys.stderr)
        print(f"Project: {project.path}", file=sys.stderr)
        print(f"Run ID: {artifacts.run_id}", file=sys.stderr)
        print(f"Report: {artifacts.report_path}", file=sys.stderr)
        if artifacts.log_path:
            print(f"Log: {artifacts.log_path}", file=sys.stderr)

    if all_failed:
        return 1

    # Suggest a Claude Code prompt to iterate on the article.
    _print_article_prompt_hint(project)

    if failures and args.verbose:
        print(f"\nWarning: {len(failures)} provider(s) failed", file=sys.stderr)
    # A requested --ingest that failed is a pipeline failure even though the
    # research succeeded (agent callers must not proceed to an empty brain).
    return handoff_rc


# Every registered subcommand. main() routes any OTHER first positional to
# instant mode (a PAID research run on that token as the topic), so a
# subcommand missing here silently becomes a paid job named after itself —
# tests assert this set matches the registered subparsers exactly.
SUBCOMMANDS = frozenset(
    {
        "create",
        "run",
        "list",
        "materials",
        "plugins",
        "advise",
        "council",
        "doctor",
        "explore",
        "links",
        "improve-topic",
        "generate-keywords",
        "add-source",
        "remove-source",
        "list-sources",
        "suggest-prompt",
    }
)


def main() -> int:
    """Main entry point for the CLI."""
    load_dotenv()

    # Parse known args first to handle the case where there's no subcommand
    parser = create_parser()

    # Decide instant-mode vs subcommand by the FIRST POSITIONAL token, skipping
    # global flags and their values. This stops "--projects-dir X list" from
    # being read as an instant run on the topic "X" (a paid job). (Review M9.)
    subcommands = SUBCOMMANDS
    value_flags = {"--projects-dir", "--log-level", "--ingest", "--materials-limit"}

    def _first_positional(tokens: list[str]) -> str | None:
        skip_next = False
        for tok in tokens:
            if skip_next:
                skip_next = False
                continue
            if tok in value_flags:
                skip_next = True
                continue
            if tok.startswith("-"):
                continue
            return tok
        return None

    tokens = sys.argv[1:]
    wants_help = "-h" in tokens or "--help" in tokens
    first = _first_positional(tokens)

    if not wants_help and first is not None and first not in subcommands:
        # Typo guard: instant mode is a PAID run, so a single bare token
        # that looks like a misspelled subcommand ("docter") must not
        # silently become a research topic. Real topics are phrases; a
        # one-word topic near a subcommand name needs explicit intent.
        import difflib

        close = difflib.get_close_matches(first, sorted(subcommands), 1, 0.75)
        if close and " " not in first:
            print(
                f"Error: unknown command {first!r} — did you mean {close[0]!r}?\n"
                f"To research {first!r} as a topic instead, use a fuller "
                f'phrase (e.g. "{first} trends") or: researchkit create '
                f'"{first}"',
                file=sys.stderr,
            )
            return 2
        # Instant mode: the first positional is the research topic.
        instant_parser = argparse.ArgumentParser(
            prog="researchkit",
            description="Instant mode: create project and run research",
        )
        instant_parser.add_argument("topic", help="The topic to research")
        instant_parser.add_argument(
            "--projects-dir",
            type=Path,
            default=_default_projects_dir(),
            help=(
                f"Directory for projects (default: $RESEARCHKIT_PROJECTS_DIR "
                f"or {PROJECTS_DIR})"
            ),
        )
        instant_parser.add_argument("--verbose", "-v", action="store_true")
        instant_parser.add_argument("--quiet", "-q", action="store_true")
        instant_parser.add_argument(
            "--log-level",
            choices=["DEBUG", "INFO", "WARNING", "ERROR"],
            default="INFO",
        )
        _add_research_args(instant_parser)

        args = instant_parser.parse_args()

        # Initialize logging
        log_dir = args.projects_dir / ".logs"
        init_app_logging(
            log_dir=log_dir,
            level=args.log_level,
            console=not args.quiet,
            console_level="WARNING" if not args.verbose else args.log_level,
        )

        # Honor --projects-dir for the actual project, not just logs. (Review M10.)
        service = SocialResearchService(projects_dir=args.projects_dir)
        return cmd_instant(args, service, args.topic)

    args = parser.parse_args()

    # Initialize logging
    projects_dir = getattr(args, "projects_dir", None) or PROJECTS_DIR
    log_dir = projects_dir / ".logs"
    init_app_logging(
        log_dir=log_dir,
        level=args.log_level,
        console=not args.quiet,
        console_level="WARNING" if not args.verbose else args.log_level,
    )

    service = SocialResearchService(projects_dir=projects_dir)

    # Registry-driven validation (lazy: only when the args carry choices that
    # used to be hardcoded argparse lists — plugin extensions widen them).
    if (
        getattr(args, "providers", None)
        or getattr(args, "site_research_sites", None)
        or getattr(args, "provider", None)
    ):
        from researchkit.plugins import get_registry

        registry = get_registry()
        for values, valid, flag in (
            (getattr(args, "providers", None), registry.provider_names, "providers"),
            (
                getattr(args, "site_research_sites", None),
                registry.connector_names,
                "site-research sites",
            ),
            (
                [args.provider] if getattr(args, "provider", None) else None,
                registry.improver_provider_names(),
                "improver provider",
            ),
        ):
            if values:
                msg = _validate_registry_choices(list(values), list(valid), flag)
                if msg:
                    print(f"Error: {msg}", file=sys.stderr)
                    return 2

    if args.command == "create":
        return cmd_create(args, service)
    elif args.command == "doctor":
        return cmd_doctor(args)
    elif args.command == "advise":
        return cmd_advise(args)
    elif args.command == "council":
        return cmd_council(args)
    elif args.command == "explore":
        return cmd_explore(args, service)
    elif args.command == "run":
        return cmd_run(args, service)
    elif args.command == "list":
        return cmd_list(args, service)
    elif args.command == "materials":
        return cmd_materials(args, service)
    elif args.command == "plugins":
        return cmd_plugins(args)
    elif args.command == "links":
        return cmd_links(args, service)
    elif args.command == "improve-topic":
        return cmd_improve_topic(args)
    elif args.command == "generate-keywords":
        return cmd_generate_keywords(args)
    elif args.command == "add-source":
        return cmd_add_source(args, service)
    elif args.command == "remove-source":
        return cmd_remove_source(args, service)
    elif args.command == "list-sources":
        return cmd_list_sources(args, service)
    elif args.command == "suggest-prompt":
        return cmd_suggest_prompt(args, service)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
