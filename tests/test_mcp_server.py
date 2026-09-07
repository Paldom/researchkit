"""Tests for the MCP server (researchkit.mcp_server)."""

from __future__ import annotations

import asyncio
import time
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
    assert tools == {
        "research",
        "list_research_projects",
        "get_research_report",
        "explore_start",
        "explore_status",
        "explore_report",
    }


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


class TestExploreOverMcp:
    """explore is 15-40 min — far past any client's tool-call timeout, so the
    surface is start / poll / read rather than one blocking call."""

    @staticmethod
    def _fake_result(project: Project, subs: list[Project] | None = None) -> Any:
        class _Result:
            pass

        result = _Result()
        result.project = project
        result.sub_projects = subs or []
        return result

    def test_start_returns_immediately_then_completes(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        import threading

        released = threading.Event()
        project = _make_project(tmp_path, "20260705_explored")

        class FakeService:
            def create_and_run_boosted(self, **kwargs: Any) -> Any:
                released.wait(timeout=10)  # still running while we poll
                return TestExploreOverMcp._fake_result(project)

        monkeypatch.setattr(mcp_module, "_get_service", lambda: FakeService())
        monkeypatch.setattr(mcp_module, "_archive_materials", lambda result: None)

        started = mcp_module.explore_start("remote topic", days=3)
        # The call must not block on the run itself.
        assert started["status"] == "running"
        assert mcp_module.explore_status(started["run_id"])["status"] == "running"

        released.set()
        for _ in range(100):
            if mcp_module.explore_status(started["run_id"])["status"] == "done":
                break
            time.sleep(0.05)
        state = mcp_module.explore_status(started["run_id"])
        assert state["status"] == "done"
        assert state["project"] == "20260705_explored"

    def test_report_refuses_while_still_running(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        import threading

        released = threading.Event()

        class FakeService:
            def create_and_run_boosted(self, **kwargs: Any) -> Any:
                released.wait(timeout=10)
                return TestExploreOverMcp._fake_result(
                    _make_project(tmp_path, "20260705_late")
                )

        monkeypatch.setattr(mcp_module, "_get_service", lambda: FakeService())
        monkeypatch.setattr(mcp_module, "_archive_materials", lambda result: None)

        started = mcp_module.explore_start("slow topic")
        with pytest.raises(ValueError, match="still running"):
            mcp_module.explore_report(started["run_id"])
        released.set()

    def test_a_failed_run_surfaces_through_status_not_a_crash(
        self, monkeypatch: Any
    ) -> None:
        class FakeService:
            def create_and_run_boosted(self, **kwargs: Any) -> Any:
                raise RuntimeError("every harness is out of quota")

        monkeypatch.setattr(mcp_module, "_get_service", lambda: FakeService())
        started = mcp_module.explore_start("doomed topic")
        for _ in range(100):
            if mcp_module.explore_status(started["run_id"])["status"] == "error":
                break
            time.sleep(0.05)
        state = mcp_module.explore_status(started["run_id"])
        assert state["status"] == "error"
        assert "out of quota" in str(state["error"])
        with pytest.raises(ValueError, match="failed"):
            mcp_module.explore_report(started["run_id"])

    def test_unknown_run_id_says_what_to_do(self) -> None:
        with pytest.raises(ValueError, match="explore_start"):
            mcp_module.explore_status("nope")

    def test_empty_topic_is_refused_before_spending(self) -> None:
        with pytest.raises(ValueError, match="topic"):
            mcp_module.explore_start("   ")

    def test_explore_uses_the_subscription_preset_and_keyless_providers(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        seen: dict[str, Any] = {}

        class FakeService:
            def create_and_run_boosted(self, **kwargs: Any) -> Any:
                seen.update(kwargs)
                return TestExploreOverMcp._fake_result(
                    _make_project(tmp_path, "20260705_preset")
                )

        monkeypatch.setattr(mcp_module, "_get_service", lambda: FakeService())
        monkeypatch.setattr(mcp_module, "_archive_materials", lambda result: None)
        started = mcp_module.explore_start("preset check")
        for _ in range(100):
            if mcp_module.explore_status(started["run_id"])["status"] != "running":
                break
            time.sleep(0.05)
        # A remote explore must cost what a local one costs: subscriptions only.
        assert seen["preset_name"] == "harness"
        assert seen["site_research_enabled"] is False
        assert seen["force_boost"] is True
        assert "perplexity" not in seen["providers"]


class TestHttpTransportConfig:
    def test_loopback_default_allows_local_hosts(self) -> None:
        mcp_module._configure_http("127.0.0.1", 8765, [])
        security = mcp_module.mcp.settings.transport_security
        assert security is not None
        assert security.enable_dns_rebinding_protection is True
        assert "127.0.0.1:8765" in security.allowed_hosts
        assert "localhost:8765" in security.allowed_hosts

    def test_extra_hosts_are_allowed_for_tunnels(self) -> None:
        mcp_module._configure_http("0.0.0.0", 9000, ["box.tail1234.ts.net:9000"])
        security = mcp_module.mcp.settings.transport_security
        assert security is not None
        assert "box.tail1234.ts.net:9000" in security.allowed_hosts
        assert "https://box.tail1234.ts.net:9000" in security.allowed_origins

    def test_star_opts_out_of_host_validation(self) -> None:
        mcp_module._configure_http("0.0.0.0", 9000, ["*"])
        security = mcp_module.mcp.settings.transport_security
        assert security is not None
        assert security.enable_dns_rebinding_protection is False

    def test_stdio_is_still_the_default_transport(self, monkeypatch: Any) -> None:
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            mcp_module.mcp, "run", lambda **kw: calls.append(kw), raising=True
        )
        monkeypatch.setattr(mcp_module, "guard_no_nested_run", lambda: None)
        mcp_module.main([])
        assert calls == [{}]  # no transport kwarg == stdio
