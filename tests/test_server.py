"""Tests for the FastAPI backend (researchkit.server.app)."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from researchkit.project import Project, ProjectConfig  # noqa: E402
from researchkit.server.app import create_app  # noqa: E402


class FakeConfigManager:
    def get_active_preset(self) -> str:
        return "default"

    def get_preset_names(self) -> list[str]:
        return ["default", "optimal"]


class FakeService:
    """Stands in for SocialResearchService in API tests."""

    def __init__(self, tmp_path: Path, fail: bool = False) -> None:
        self.tmp_path = tmp_path
        self.fail = fail
        self.calls: list[dict[str, Any]] = []
        self.config_manager = FakeConfigManager()

    def create_and_run_project(
        self, *, progress: Any = None, **kwargs: Any
    ) -> tuple[Project, Any]:
        self.calls.append(kwargs)
        if progress:
            progress({"stage": "provider_start", "message": "Starting openai"})
            progress({"stage": "provider_done", "message": "openai: success"})
        if self.fail:
            raise RuntimeError("boom")
        folder = self.tmp_path / "20260705_researchkit"
        folder.mkdir(exist_ok=True)
        (folder / "report.md").write_text("# Report", encoding="utf-8")
        project = Project(
            path=folder,
            config=ProjectConfig(topic=kwargs.get("topic", "t")),
            created_at=datetime(2026, 7, 5),
        )
        return project, object()


def _wait_done(client: TestClient, run_id: str, timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get(f"/api/research/{run_id}").json()
        if status["status"] != "running":
            return dict(status)
        time.sleep(0.02)
    raise AssertionError("run did not finish in time")


def test_health_and_config(tmp_path: Path) -> None:
    client = TestClient(create_app(FakeService(tmp_path)))  # type: ignore[arg-type]
    assert client.get("/api/health").json()["status"] == "ok"
    cfg = client.get("/api/config").json()
    assert cfg["active_preset"] == "default"
    assert "openai" in cfg["default_providers"]


def test_research_run_and_sse_stream(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    client = TestClient(create_app(service))  # type: ignore[arg-type]

    resp = client.post("/api/research", json={"topic": "ai agents", "days": 3})
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]

    status = _wait_done(client, run_id)
    assert status["status"] == "done"
    assert status["project"] == "20260705_researchkit"
    assert service.calls[0]["days"] == 3
    assert service.calls[0]["providers"] == ["openai", "gemini", "grok", "perplexity"]

    with client.stream("GET", f"/api/research/{run_id}/events") as stream:
        body = "".join(stream.iter_text())
    assert "event: progress" in body
    assert '"provider_start"' in body
    assert "event: done" in body
    assert "20260705_researchkit" in body


def test_research_failure_reports_error(tmp_path: Path) -> None:
    client = TestClient(create_app(FakeService(tmp_path, fail=True)))  # type: ignore[arg-type]
    run_id = client.post("/api/research", json={"topic": "ai agents"}).json()["run_id"]
    status = _wait_done(client, run_id)
    assert status["status"] == "error"
    assert "boom" in status["error"]

    with client.stream("GET", f"/api/research/{run_id}/events") as stream:
        body = "".join(stream.iter_text())
    assert "event: error" in body


def test_validation_rejects_unknown_provider_and_source(tmp_path: Path) -> None:
    client = TestClient(create_app(FakeService(tmp_path)))  # type: ignore[arg-type]
    resp = client.post(
        "/api/research", json={"topic": "ai agents", "providers": ["bogus"]}
    )
    assert resp.status_code == 422
    resp = client.post(
        "/api/research", json={"topic": "ai agents", "sources": ["bogus"]}
    )
    assert resp.status_code == 422


def test_unknown_run_and_project_return_404(tmp_path: Path) -> None:
    client = TestClient(create_app(FakeService(tmp_path)))  # type: ignore[arg-type]
    assert client.get("/api/research/nope").status_code == 404
    assert client.get("/api/projects/nope/report").status_code == 404


def test_projects_listing_and_report(tmp_path: Path, monkeypatch: Any) -> None:
    import researchkit.server.app as app_module

    folder = tmp_path / "20260705_topic"
    folder.mkdir()
    (folder / "report.md").write_text("# The Report", encoding="utf-8")
    project = Project(
        path=folder,
        config=ProjectConfig(topic="topic"),
        created_at=datetime(2026, 7, 5),
    )
    monkeypatch.setattr(app_module, "list_projects", lambda _dir: [project])

    client = TestClient(create_app(FakeService(tmp_path)))  # type: ignore[arg-type]
    listing = client.get("/api/projects").json()
    assert listing[0]["name"] == "20260705_topic"
    assert listing[0]["has_report"] is True

    report = client.get("/api/projects/20260705_topic/report")
    assert report.status_code == 200
    assert report.text == "# The Report"


def test_reconnect_after_completion_gets_terminal_event(tmp_path: Path) -> None:
    """A second SSE subscriber (drained queue) must not hang on keep-alives."""
    import researchkit.server.app as app_module

    client = TestClient(create_app(FakeService(tmp_path)))  # type: ignore[arg-type]
    run_id = client.post("/api/research", json={"topic": "ai agents"}).json()["run_id"]
    _wait_done(client, run_id)

    with client.stream("GET", f"/api/research/{run_id}/events") as first:
        "".join(first.iter_text())

    original = app_module._SSE_HEARTBEAT_SECONDS
    app_module._SSE_HEARTBEAT_SECONDS = 0.05
    try:
        with client.stream("GET", f"/api/research/{run_id}/events") as second:
            body = "".join(second.iter_text())
    finally:
        app_module._SSE_HEARTBEAT_SECONDS = original
    assert "event: done" in body


def test_active_run_cap_returns_429(tmp_path: Path) -> None:
    import threading as _threading

    release = _threading.Event()

    class BlockingService(FakeService):
        def create_and_run_project(self, *, progress: Any = None, **kwargs: Any):
            release.wait(timeout=10)
            return super().create_and_run_project(progress=progress, **kwargs)

    client = TestClient(create_app(BlockingService(tmp_path)))  # type: ignore[arg-type]
    try:
        for _ in range(4):
            assert (
                client.post("/api/research", json={"topic": "ai agents"}).status_code
                == 202
            )
        resp = client.post("/api/research", json={"topic": "ai agents"})
        assert resp.status_code == 429
    finally:
        release.set()


def test_duplicate_providers_are_deduped(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    client = TestClient(create_app(service))  # type: ignore[arg-type]
    run_id = client.post(
        "/api/research",
        json={"topic": "ai agents", "providers": ["openai", "openai", "gemini"]},
    ).json()["run_id"]
    _wait_done(client, run_id)
    assert service.calls[0]["providers"] == ["openai", "gemini"]


def test_auth_token_guards_api(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("RESEARCHKIT_AUTH_TOKEN", "sekrit")
    client = TestClient(create_app(FakeService(tmp_path)))  # type: ignore[arg-type]
    assert client.get("/api/health").status_code == 200  # health stays open
    assert client.get("/api/projects").status_code == 401
    assert (
        client.get(
            "/api/projects", headers={"Authorization": "Bearer sekrit"}
        ).status_code
        == 200
    )
