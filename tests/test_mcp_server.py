"""Tests for the MCP server (researchkit.mcp_server)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

import researchkit.mcp_server as mcp_module
from researchkit.project import Project, ProjectConfig


def _make_project(tmp_path: Path, name: str, with_report: bool = True) -> Project:
    folder = tmp_path / name
    folder.mkdir(exist_ok=True)
    if with_report:
        (folder / "report.md").write_text("# Stored Report", encoding="utf-8")
    return Project(
        path=folder,
        config=ProjectConfig(topic="stored topic"),
        created_at=datetime(2026, 7, 5),
    )


def test_tools_are_registered() -> None:
    tools = {t.name for t in asyncio.run(mcp_module.mcp.list_tools())}
    assert tools == {"research", "list_research_projects", "get_research_report"}


def test_research_tool_runs_service(monkeypatch: Any, tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    class FakeArtifacts:
        report_markdown = "# Fresh Report"

    class FakeService:
        def create_and_run_project(self, **kwargs: Any) -> tuple[Project, Any]:
            calls.append(kwargs)
            return _make_project(tmp_path, "20260705_fresh"), FakeArtifacts()

    monkeypatch.setattr(mcp_module, "_get_service", lambda: FakeService())
    out = mcp_module.research("ai agents", days=3, providers=["openai"])
    assert "# Fresh Report" in out
    assert "20260705_fresh" in out
    assert calls[0]["topic"] == "ai agents"
    assert calls[0]["days"] == 3


def test_list_and_get_report(monkeypatch: Any, tmp_path: Path) -> None:
    project = _make_project(tmp_path, "20260705_stored")
    monkeypatch.setattr(mcp_module, "list_projects", lambda _dir: [project])

    listing = mcp_module.list_research_projects()
    assert listing[0]["name"] == "20260705_stored"
    assert listing[0]["has_report"] is True

    assert mcp_module.get_research_report("20260705_stored") == "# Stored Report"


def test_get_report_unknown_project(monkeypatch: Any) -> None:
    monkeypatch.setattr(mcp_module, "list_projects", lambda _dir: [])
    with pytest.raises(ValueError, match="Unknown project"):
        mcp_module.get_research_report("nope")


def test_get_report_without_report(monkeypatch: Any, tmp_path: Path) -> None:
    project = _make_project(tmp_path, "20260705_bare", with_report=False)
    monkeypatch.setattr(mcp_module, "list_projects", lambda _dir: [project])
    with pytest.raises(ValueError, match="no report yet"):
        mcp_module.get_research_report("20260705_bare")
