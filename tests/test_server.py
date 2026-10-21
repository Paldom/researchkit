"""Tests for the FastAPI backend (researchkit.server.app)."""

from __future__ import annotations

import json
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
    def __init__(self) -> None:
        self.active = "default"

    def get_active_preset(self) -> str:
        return self.active

    def get_preset_names(self) -> list[str]:
        return ["default", "optimal"]

    def set_active_preset(self, name: str) -> None:
        self.active = name


class FakeService:
    """Stands in for SocialResearchService in API tests."""

    def __init__(self, tmp_path: Path, fail: bool = False) -> None:
        self.tmp_path = tmp_path
        self.fail = fail
        self.calls: list[dict[str, Any]] = []
        self.config_manager = FakeConfigManager()

    def create_and_run_boosted(
        self, topic: str, *, progress: Any = None, **kwargs: Any
    ) -> Any:
        self.boost_calls: list[dict[str, Any]] = getattr(self, "boost_calls", [])
        self.boost_calls.append({"topic": topic, **kwargs})
        if progress:
            progress({"stage": "council_start", "message": "Convening council"})
        project, _ = self.create_and_run_project(progress=None, topic=topic)

        class Boosted:
            def __init__(self, proj: Project) -> None:
                self.project = proj

        return Boosted(project)

    def create_project(self, **kwargs: Any) -> Project:
        self.calls.append({"create_only": True, **kwargs})
        folder = self.tmp_path / "20260706_created"
        folder.mkdir(exist_ok=True)
        return Project(
            path=folder,
            config=ProjectConfig(topic=kwargs.get("topic", "t")),
            created_at=datetime(2026, 7, 6),
        )

    def add_user_source(
        self, project: Project, source: str, title: str | None = None
    ) -> None:
        self.user_sources: list[str] = getattr(self, "user_sources", [])
        if source.startswith("bogus"):
            raise ValueError("bad url")
        if source.startswith("http"):
            self.user_sources.append(source)
        else:
            from pathlib import Path as _P

            path = _P(source)
            self.user_sources.append(f"file:{path.name}:{len(path.read_text())}")

    def run_project(
        self, project: Project, *, progress: Any = None, **kwargs: Any
    ) -> Any:
        self.run_calls: list[str] = getattr(self, "run_calls", [])
        self.run_calls.append(project.name)
        (project.path / "report.md").write_text("# R", encoding="utf-8")
        return object()

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
    # No providers in the request: the preset decides (harness presets declare
    # a zero-key set). The server used to substitute every known provider.
    assert service.calls[0]["providers"] is None

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


def _project_with_outputs(tmp_path: Path) -> Project:
    folder = tmp_path / "20260706_full"
    folder.mkdir(exist_ok=True)
    (folder / "report.md").write_text("# Full Report", encoding="utf-8")
    (folder / "run.log").write_text("log line", encoding="utf-8")
    (folder / "result.json").write_text(
        json.dumps(
            {
                "topic": "t",
                "provider_results": [
                    {
                        "provider": "openai",
                        "sources": [{"url": "https://a.test/x", "title": "A"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return Project(
        path=folder, config=ProjectConfig(topic="t"), created_at=datetime(2026, 7, 6)
    )


def test_boost_routes_to_boosted_run(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    client = TestClient(create_app(service))  # type: ignore[arg-type]
    run_id = client.post(
        "/api/research", json={"topic": "ai agents", "boost": True}
    ).json()["run_id"]
    _wait_done(client, run_id)
    assert service.boost_calls[0]["force_boost"] is True
    # Unset = the preset decides (harness presets turn site research off).
    assert service.boost_calls[0]["site_research_enabled"] is None

    with client.stream("GET", f"/api/research/{run_id}/events") as stream:
        body = "".join(stream.iter_text())
    assert "council_start" in body


def test_user_urls_route_creates_then_runs(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    client = TestClient(create_app(service))  # type: ignore[arg-type]
    run_id = client.post(
        "/api/research",
        json={
            "topic": "ai agents",
            "user_urls": ["https://ok.test/a", "bogus://x"],
            "keywords": ["k1"],
        },
    ).json()["run_id"]
    status = _wait_done(client, run_id)
    assert status["status"] == "done"
    assert service.user_sources == ["https://ok.test/a"]  # bad URL skipped
    assert service.run_calls == ["20260706_created"]
    assert service.calls[0]["keywords"] == ["k1"]


def test_project_output_endpoints(tmp_path: Path, monkeypatch: Any) -> None:
    import researchkit.server.app as app_module

    project = _project_with_outputs(tmp_path)
    monkeypatch.setattr(app_module, "list_projects", lambda _dir: [project])
    client = TestClient(create_app(FakeService(tmp_path)))  # type: ignore[arg-type]

    assert client.get("/api/projects/20260706_full/log").text == "log line"
    assert client.get("/api/projects/20260706_full/result").json()["topic"] == "t"
    prompt = client.get("/api/projects/20260706_full/prompt")
    assert prompt.status_code == 200
    assert "report.md" in prompt.text

    links = client.get("/api/projects/20260706_full/links?mode=strict").json()
    assert links["citations"]["summary"]["total_occurrences"] == 1
    assert client.get("/api/projects/20260706_full/links?mode=bad").status_code == 422


def test_handoff_artifact_endpoints(tmp_path: Path, monkeypatch: Any) -> None:
    """article.md, skill/SKILL.md and the brain are served for the UI tabs."""
    import researchkit.server.app as app_module

    project = _project_with_outputs(tmp_path)
    monkeypatch.setattr(app_module, "list_projects", lambda _dir: [project])
    client = TestClient(create_app(FakeService(tmp_path)))  # type: ignore[arg-type]
    base = "/api/projects/20260706_full"

    # Nothing generated yet: 404s the UI turns into "not available" hints.
    assert client.get(f"{base}/article").status_code == 404
    assert client.get(f"{base}/skill").status_code == 404
    assert client.get(f"{base}/brain").status_code == 404

    project.save_article("# Headline\n\nBody")
    project.save_skill("---\nname: x\n---\nbody\n")
    notes = project.brain_dir / "notes" / "topics"
    notes.mkdir(parents=True)
    (project.brain_dir / "index.md").write_text("# Brain index", encoding="utf-8")
    (notes / "t1.md").write_text(
        '---\ntitle: "T"\ntype: "topic"\n---\nx', encoding="utf-8"
    )
    (tmp_path / "secret.md").write_text("nope", encoding="utf-8")

    assert client.get(f"{base}/article").text.startswith("# Headline")
    assert client.get(f"{base}/skill").text.startswith("---\nname: x")
    brain = client.get(f"{base}/brain").json()
    assert brain["index"] == "# Brain index"
    assert brain["notes"] == [
        {
            "path": "notes/topics/t1.md",
            "slug": "t1",
            "title": "T",
            "type": "topic",
            "url": "",
        }
    ]
    assert client.get(f"{base}/brain/note", params={"path": "notes/topics/t1.md"}).text
    # The brain directory is the trust boundary for note paths.
    escaped = client.get(f"{base}/brain/note", params={"path": "../../secret.md"})
    assert escaped.status_code == 404
    listing = client.get("/api/projects").json()[0]
    assert listing["has_article"] and listing["has_skill"] and listing["has_brain"]


def test_improve_topic_and_keywords_endpoints(tmp_path: Path, monkeypatch: Any) -> None:
    import researchkit.prompt_improver as pi

    class FakeImprover:
        def __init__(self, *a: Any, **k: Any) -> None: ...

        def improve_topic(self, topic: str) -> str:
            return f"improved {topic}"

        def generate_keywords(self, topic: str, count: int = 10) -> list[str]:
            return [f"kw{i}" for i in range(count)]

    monkeypatch.setattr(pi, "PromptImprover", FakeImprover)
    client = TestClient(create_app(FakeService(tmp_path)))  # type: ignore[arg-type]
    assert (
        client.post("/api/improve-topic", json={"topic": "ai agents"}).json()["topic"]
        == "improved ai agents"
    )
    kws = client.post("/api/keywords", json={"topic": "ai agents", "count": 3}).json()
    assert kws["keywords"] == ["kw0", "kw1", "kw2"]


def test_set_preset_endpoint(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    client = TestClient(create_app(service))  # type: ignore[arg-type]
    resp = client.post("/api/config/preset", json={"preset": "optimal"})
    assert resp.json()["active_preset"] == "optimal"
    assert client.post("/api/config/preset", json={"preset": "nope"}).status_code == 422


def test_user_files_and_texts_become_sources(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    client = TestClient(create_app(service))  # type: ignore[arg-type]
    run_id = client.post(
        "/api/research",
        json={
            "topic": "ai agents",
            "user_files": [
                {"name": "My Notes!.md", "content": "hello notes"},
                {"name": "../evil.sh", "content": "payload"},
            ],
            "user_texts": ["pasted context", "   "],
        },
    ).json()["run_id"]
    status = _wait_done(client, run_id)
    assert status["status"] == "done"
    assert "file:my-notes.md:11" in service.user_sources
    # traversal name sanitized to a safe stem + forced text extension
    assert any(s.startswith("file:evil.txt:") for s in service.user_sources)
    assert "file:note-1.md:14" in service.user_sources
    # whitespace-only text dropped by validation
    assert not any(":note-2" in s for s in service.user_sources)


def test_days_beyond_thirty_accepted_and_capped(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    client = TestClient(create_app(service))  # type: ignore[arg-type]
    run_id = client.post(
        "/api/research", json={"topic": "ai agents", "days": 180}
    ).json()["run_id"]
    _wait_done(client, run_id)
    assert service.calls[0]["days"] == 180
    assert (
        client.post(
            "/api/research", json={"topic": "ai agents", "days": 366}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/research",
            json={"topic": "t3", "user_texts": ["x" * 100_001]},
        ).status_code
        == 422
    )


def test_shared_brain_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PLAN B.6: create, list, read a named brain; names only over HTTP."""
    pytest.importorskip("brainkit.brain")
    monkeypatch.setenv("RESEARCHKIT_BRAINS_DIR", str(tmp_path / "brains"))
    client = TestClient(create_app(FakeService(tmp_path)))  # type: ignore[arg-type]

    created = client.post("/api/brains", json={"name": "team"})
    assert created.status_code == 200 and created.json()["name"] == "team"
    assert [b["name"] for b in client.get("/api/brains").json()] == ["team"]
    assert "index" in client.get("/api/brains/team").json()
    assert client.post("/api/brains", json={"name": "../etc"}).status_code == 422
    assert (
        client.post(
            "/api/research", json={"topic": "ai agents", "brain": "/tmp/x"}
        ).status_code
        == 422
    )
