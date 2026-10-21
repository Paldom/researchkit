"""MCP server exposing researchkit as tools for AI agents.

Runs over stdio so any MCP client (Claude Code, Claude Desktop, etc.) can
call researchkit as a single research tool:

    {"mcpServers": {"researchkit": {"command": "researchkit-mcp"}}}

Requires the ``mcp`` extra: ``pip install "researchkit[mcp]"``.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from researchkit.project import PROJECTS_DIR, Project, list_projects
from researchkit.safe_io import guard_no_nested_run
from researchkit.service import SocialResearchService

logger = logging.getLogger(__name__)

try:
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings
except ImportError as e:  # pragma: no cover - exercised only without the extra
    raise SystemExit(
        'researchkit-mcp requires the MCP extra: pip install "researchkit[mcp]"'
    ) from e

mcp = FastMCP(
    "researchkit",
    instructions=(
        "Multi-provider AI research: give a topic, get one citation-backed "
        "markdown report synthesized from parallel AI web searches. "
        "The `research` tool is slow (typically 1-5 minutes) — call it once "
        "and wait; do not retry while a call is in flight."
    ),
)

_service: SocialResearchService | None = None


def _get_service() -> SocialResearchService:
    """Lazily build the service (monkeypatch point for tests)."""
    global _service
    if _service is None:
        _service = SocialResearchService()
    return _service


def _find_project(project_name: str) -> Project:
    """Resolve a project by exact name (no path semantics accepted)."""
    for project in list_projects(PROJECTS_DIR):
        if project.name == project_name:
            return project
    raise ValueError(
        f"Unknown project {project_name!r}. "
        "Use list_research_projects to see available names."
    )


@mcp.tool()
def research(
    topic: str,
    days: int = 7,
    providers: list[str] | None = None,
    preset: str | None = None,
    brain: str | None = None,
) -> str:
    """Research a topic across AI web-search providers and return a cited report.

    Runs the full researchkit pipeline: queries each provider concurrently
    (each performs live web/social search), then synthesizes one
    citation-backed markdown report. Slow: expect 1-5 minutes.

    Args:
        topic: What to research, e.g. "developer sentiment on Bun vs Node".
        days: Lookback window in days for recency filtering (default 7).
        providers: Providers to query; defaults to openai, gemini, grok and
            perplexity. Others available: tavily, claude, github, glm, kimi, brave, openalex.
        preset: models.yaml preset name (default: the active preset).
        brain: A named shared brain (see ``list_brains``) to start from and
            feed; the run cites what the brain already knows and is ingested
            into it afterwards. Names only, not paths.

    Returns:
        The breadth-first markdown report: TL;DR, In brief, finding
        sections, Sources.
    """
    service = _get_service()
    project, artifacts = service.create_and_run_project(
        topic=topic,
        days=days,
        providers=providers,
        preset_name=preset,
        brain=_named_brain(brain),
    )
    logger.info("MCP research run finished: %s", project.name)
    return f"<!-- project: {project.name} -->\n{artifacts.report_markdown}"


def _named_brain(name: str | None) -> Path | None:
    """Resolve a brain NAME (paths refused: a remote caller must not pick
    where the server writes)."""
    if not name:
        return None
    if "/" in name or name.startswith((".", "~")):
        raise ValueError("brain must be a name from list_brains, not a path")
    from researchkit.brains import resolve_brain

    return resolve_brain(name)


@mcp.tool()
def list_brains() -> list[dict[str, Any]]:
    """Named shared brains (each fed by many research runs) with their counts."""
    from researchkit.brains import list_brains as _list

    return [info.to_dict() for info in _list()]


@mcp.tool()
def brain_context(brain: str, query: str, limit: int = 8) -> list[dict[str, Any]]:
    """What a shared brain already knows about ``query``: cited source notes.

    Returns url, title, snippet and published date per hit, so an agent can
    answer from earlier research before starting a new run.
    """
    from researchkit.brains import prior_knowledge

    path = _named_brain(brain)
    assert path is not None
    return [
        {"url": h.url, "title": h.title, "snippet": h.snippet, "published": h.published}
        for h in prior_knowledge(path, query, limit=limit)
    ]


# --- explore: boosted, subscription-only research -------------------------
#
# The five logged-in coding-agent CLIs plus every keyless provider — the same
# set `researchkit explore` uses, so a remote explore costs exactly what a local
# one does (nothing beyond the subscriptions already paid for).
_EXPLORE_PRESET = "harness"


def _explore_providers() -> list[str]:
    """The harness preset's declared zero-key provider set (models.yaml)."""
    from researchkit.system_config import SystemConfigManager

    return SystemConfigManager().resolve_effective_models(_EXPLORE_PRESET).providers


@dataclass
class _ExploreRun:
    """State of one background explore, kept in memory for polling."""

    run_id: str
    topic: str
    status: str = "running"  # running | done | error
    stage: str = "queued"
    project: str | None = None
    error: str | None = None
    started_at: float = field(default_factory=time.monotonic)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "topic": self.topic,
            "status": self.status,
            "stage": self.stage,
            "project": self.project,
            "error": self.error,
            "elapsed_seconds": int(time.monotonic() - self.started_at),
        }


_explore_runs: OrderedDict[str, _ExploreRun] = OrderedDict()
_explore_lock = threading.Lock()
_MAX_TRACKED_EXPLORES = 50


@mcp.tool()
def explore_start(topic: str, days: int = 7, materials: bool = True) -> dict[str, Any]:
    """Start a boosted, subscription-only research run and return immediately.

    An explore takes 15-40 minutes: a council refines and decomposes the topic,
    parallel sub-investigations research it, and a super-summary synthesizes.
    That is far longer than any MCP client will hold a tool call open, so this
    starts the work and hands back a run_id — poll `explore_status`, then read
    `explore_report`. Runs on logged-in CLI subscriptions; no API keys.

    Args:
        topic: What to research.
        days: Lookback window in days (default 7).
        materials: Archive every cited source into the project's materials/.

    Returns:
        The run_id plus the initial status. Nothing is researched yet.
    """
    if not topic.strip():
        raise ValueError("topic must not be empty")

    run_id = uuid.uuid4().hex[:12]
    state = _ExploreRun(run_id=run_id, topic=topic.strip())
    with _explore_lock:
        _explore_runs[run_id] = state
        while len(_explore_runs) > _MAX_TRACKED_EXPLORES:
            _explore_runs.popitem(last=False)

    def progress(event: dict[str, Any]) -> None:
        state.stage = str(event.get("stage") or state.stage)

    def work() -> None:
        try:
            result = _get_service().create_and_run_boosted(
                topic=state.topic,
                days=days,
                providers=_explore_providers(),
                preset_name=_EXPLORE_PRESET,
                site_research_enabled=False,
                force_boost=True,
                progress=progress,
            )
            state.project = result.project.name
            if materials:
                _archive_materials(result)
            state.status = "done"
            state.stage = "done"
        except Exception as e:  # surfaced through explore_status, never raised here
            logger.exception("Explore run failed")
            state.status = "error"
            state.error = str(e)[:500]

    threading.Thread(target=work, name=f"explore-{run_id}", daemon=True).start()
    return state.to_dict()


def _archive_materials(result: Any) -> None:
    """Download cited sources for a finished explore; never fatal.

    A boosted parent cites its sub-reports rather than the web, so the sources
    live in the sub-projects — archive there. The report already exists by this
    point, so a failed archive degrades the pack, it does not fail the run.
    """
    from researchkit.materials import download_materials

    targets = result.sub_projects or [result.project]
    for target in targets:
        try:
            download_materials(target)
        except Exception as e:
            logger.warning("Materials archive failed for %s: %s", target.name, e)


@mcp.tool()
def explore_status(run_id: str) -> dict[str, Any]:
    """Check a running explore: status, current stage, elapsed time.

    Poll this every minute or so — an explore takes 15-40 minutes. When status
    is "done", read the write-up with `explore_report`.

    Args:
        run_id: The id returned by explore_start.
    """
    with _explore_lock:
        state = _explore_runs.get(run_id)
    if state is None:
        raise ValueError(
            f"Unknown run_id {run_id!r}. Start one with explore_start; ids are "
            "kept in memory, so a server restart forgets them (finished work "
            "is still on disk — see list_research_projects)."
        )
    return state.to_dict()


@mcp.tool()
def explore_report(run_id: str) -> str:
    """Return the finished explore's write-up (super-summary, then report).

    Args:
        run_id: The id returned by explore_start.
    """
    state = explore_status(run_id)
    if state["status"] == "error":
        raise ValueError(f"That explore failed: {state['error']}")
    if state["status"] != "done":
        raise ValueError(
            f"That explore is still running ({state['stage']}, "
            f"{state['elapsed_seconds']}s elapsed). Poll explore_status."
        )
    project = _find_project(str(state["project"]))
    for path in (project.super_summary_path, project.report_path):
        if path.is_file():
            return path.read_text(encoding="utf-8")
    raise ValueError(f"Project {project.name!r} finished without a written report.")


@mcp.tool()
def list_research_projects() -> list[dict[str, Any]]:
    """List stored research projects, newest first.

    Returns:
        One entry per project with name, topic, days, providers, created_at
        and has_report. Pass a name to get_research_report to read a report.
    """
    return [p.summary() for p in list_projects(PROJECTS_DIR)]


@mcp.tool()
def get_research_report(project_name: str) -> str:
    """Return the markdown report of a previously run project.

    Args:
        project_name: Exact project folder name from list_research_projects.

    Returns:
        The stored report markdown.
    """
    project = _find_project(project_name)
    report = project.report_path
    if not report.is_file():
        raise ValueError(
            f"Project {project_name!r} has no report yet — run it first "
            "(researchkit run) or use the research tool."
        )
    return report.read_text(encoding="utf-8")


@mcp.tool()
def get_research_artifact(project_name: str, artifact: str, note: str = "") -> str:
    """Return one of a project's handoff artifacts as text.

    Args:
        project_name: Exact project folder name from list_research_projects.
        artifact: "article" (article.md), "skill" (skill/SKILL.md) or "brain".
        note: For "brain" only — a brain-relative markdown path such as
            "notes/topics/<slug>.md" (the index is returned when empty; every
            note's path is listed there and in the index of each level).

    Returns:
        The artifact's markdown. list_research_projects reports has_article,
        has_skill and has_brain per project.
    """
    from researchkit.brain import read_brain_file

    project = _find_project(project_name)
    if artifact == "brain":
        text = read_brain_file(project.brain_dir, note or "index.md")
        if text is None:
            raise ValueError(
                f"Project {project_name!r} has no brain note {note or 'index.md'!r}"
            )
        return text
    paths = {"article": project.article_path, "skill": project.skill_path}
    if artifact not in paths:
        raise ValueError("artifact must be one of: article, skill, brain")
    if not paths[artifact].is_file():
        raise ValueError(
            f"Project {project_name!r} has no {artifact} — the writer skipped it "
            "or the run predates handoff artifacts."
        )
    return paths[artifact].read_text(encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="researchkit-mcp",
        description=(
            "MCP server exposing researchkit. Default transport is stdio (the "
            "client spawns this process). Pass --http to serve Streamable HTTP "
            "so a REMOTE client can reach it."
        ),
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve Streamable HTTP on /mcp instead of stdio",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address for --http (default: 127.0.0.1, local only)",
    )
    parser.add_argument("--port", type=int, default=8765, help="Port for --http")
    parser.add_argument(
        "--allow-host",
        action="append",
        default=[],
        metavar="HOST[:PORT]",
        help=(
            "Extra Host/Origin values to accept (repeatable). Needed when you "
            "reach the server by a name it can't guess — a tunnel or LAN "
            "hostname. Use '*' to accept any."
        ),
    )
    return parser


def _configure_http(host: str, port: int, allow: list[str]) -> None:
    """Point FastMCP at host:port and set the DNS-rebinding allowlist.

    There is no authentication here by design. The mitigations that remain are
    binding loopback by default and validating Host/Origin, so a browser on the
    operator's machine cannot be tricked into driving this server.
    """
    mcp.settings.host = host
    mcp.settings.port = port

    allowed = [f"{host}:{port}", f"localhost:{port}", f"127.0.0.1:{port}", *allow]
    if "*" in allow:
        # Explicitly opted out: any Host/Origin, e.g. behind a tunnel that
        # rewrites both.
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    else:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed,
            allowed_origins=[f"http://{a}" for a in allowed]
            + [f"https://{a}" for a in allowed],
        )
    mcp.settings.transport_security = security

    if host not in ("127.0.0.1", "localhost", "::1"):
        # Not a refusal — the operator asked for this — but it must be said out
        # loud: an explore spends their subscription quota, and there is no
        # token in front of it.
        print(
            f"WARNING: serving MCP on {host}:{port} with NO AUTHENTICATION.\n"
            "         Anyone who can reach this port can start research runs "
            "that spend your subscription quota.\n"
            "         Prefer a private tunnel (Tailscale, ssh -L) over a public "
            "bind.",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> None:
    """Entry point for the ``researchkit-mcp`` console script."""
    nested = guard_no_nested_run()
    if nested is not None:
        raise SystemExit(nested)
    os.environ.setdefault("RESEARCHKIT_ORIGIN", "mcp")
    load_dotenv()  # provider API keys from ./.env, like the CLI

    args = _build_parser().parse_args(argv)
    if not args.http:
        # stdio: stdout carries JSON-RPC framing and nothing else.
        mcp.run()
        return

    _configure_http(args.host, args.port, args.allow_host)
    print(
        f"researchkit MCP (Streamable HTTP) on http://{args.host}:{args.port}/mcp",
        file=sys.stderr,
    )
    mcp.run(transport="streamable-http")


if __name__ == "__main__":  # pragma: no cover
    main()
