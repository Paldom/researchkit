"""MCP server exposing researchkit as tools for AI agents.

Runs over stdio so any MCP client (Claude Code, Claude Desktop, etc.) can
call researchkit as a single research tool:

    {"mcpServers": {"researchkit": {"command": "researchkit-mcp"}}}

Requires the ``mcp`` extra: ``pip install "researchkit[mcp]"``.
"""

from __future__ import annotations

import logging
from typing import Any

from dotenv import load_dotenv

from researchkit.project import PROJECTS_DIR, Project, list_projects
from researchkit.service import SocialResearchService

logger = logging.getLogger(__name__)

try:
    from mcp.server.fastmcp import FastMCP
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
) -> str:
    """Research a topic across AI web-search providers and return a cited report.

    Runs the full researchkit pipeline: queries each provider concurrently
    (each performs live web/social search), then synthesizes one
    citation-backed markdown report. Slow: expect 1-5 minutes.

    Args:
        topic: What to research, e.g. "developer sentiment on Bun vs Node".
        days: Lookback window in days for recency filtering (default 7).
        providers: Providers to query; defaults to openai, gemini, grok and
            perplexity. Others available: tavily, claude, github, glm, kimi.
        preset: models.yaml preset name (default: the active preset).

    Returns:
        The full markdown report, including per-provider findings, the
        consolidated analysis, and source URLs.
    """
    service = _get_service()
    project, artifacts = service.create_and_run_project(
        topic=topic,
        days=days,
        providers=providers,
        preset_name=preset,
    )
    logger.info("MCP research run finished: %s", project.name)
    return f"<!-- project: {project.name} -->\n{artifacts.report_markdown}"


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


def main() -> None:
    """Entry point for the ``researchkit-mcp`` console script (stdio)."""
    load_dotenv()  # provider API keys from ./.env, like the CLI
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
