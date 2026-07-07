"""FastAPI backend serving the researchkit REST/SSE API and the built web UI.

Minimal by design: research runs execute in daemon threads with an in-memory
run registry, progress streams over Server-Sent Events, and the built React
frontend (web/dist) is served from the same origin. Single-process only —
front it with a real ASGI deployment (and auth) if you expose it beyond
localhost.

Requires the ``server`` extra: ``pip install "researchkit[server]"``.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import secrets
import threading
import uuid
from collections import OrderedDict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from researchkit import __version__
from researchkit.project import PROJECTS_DIR, Project, list_projects
from researchkit.service import SocialResearchService

logger = logging.getLogger(__name__)


def _known_providers() -> tuple[str, ...]:
    """Registered provider names (builtins + active plugins)."""
    from researchkit.plugins import get_registry

    return tuple(get_registry().provider_names)


def _known_connectors() -> tuple[str, ...]:
    from researchkit.plugins import get_registry

    return tuple(get_registry().connector_names)


_MAX_TRACKED_RUNS = 100
_MAX_ACTIVE_RUNS = 4
_SSE_HEARTBEAT_SECONDS = 15.0


class UserFileIn(BaseModel):
    """A text document supplied by the user (content sent inline)."""

    name: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=200_000)


class ResearchIn(BaseModel):
    """Request body for POST /api/research."""

    topic: str = Field(min_length=3, max_length=500)
    days: int = Field(default=7, ge=1, le=365)
    providers: list[str] | None = None
    preset: str | None = None
    sources: list[str] = Field(default_factory=lambda: ["social", "web"])
    keywords: list[str] = Field(default_factory=list, max_length=25)
    include_raw: bool = True
    site_research: bool = True
    site_research_sites: list[str] | None = None
    # Boost: LLM council refines the topic and may fan out into parallel
    # sub-projects (manual keywords and custom sources are ignored there).
    boost: bool = False
    user_urls: list[str] = Field(default_factory=list, max_length=25)
    user_files: list[UserFileIn] = Field(default_factory=list, max_length=10)
    user_texts: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("user_texts")
    @classmethod
    def _cap_text_lengths(cls, texts: list[str]) -> list[str]:
        for text in texts:
            if len(text) > 100_000:
                raise ValueError("each text source is capped at 100k characters")
        return [t for t in texts if t.strip()]


class TopicIn(BaseModel):
    """Request body for improve-topic / keywords helpers."""

    topic: str = Field(min_length=3, max_length=500)
    count: int = Field(default=10, ge=1, le=25)


class PresetIn(BaseModel):
    """Request body for switching the active preset."""

    preset: str = Field(min_length=1, max_length=100)


class RunStatus(BaseModel):
    """Response body for GET /api/research/{run_id}."""

    status: str
    project: str | None = None
    error: str | None = None


class _RunState:
    """Mutable state for one background research run."""

    def __init__(self) -> None:
        self.events: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.status = "running"
        self.project: str | None = None
        self.error: str | None = None


_SOURCE_EXTENSIONS = {".md", ".markdown", ".txt", ".rst"}


def _safe_source_filename(name: str) -> str:
    """Sanitized filename for an uploaded text document."""
    from researchkit.utils import slugify

    raw = Path(name.replace("\\", "/")).name
    suffix = Path(raw).suffix.lower()
    if suffix not in _SOURCE_EXTENSIONS:
        suffix = ".txt"
    stem = slugify(Path(raw).stem, max_length=60) or "document"
    return f"{stem}{suffix}"


def _attach_document_sources(run_svc: Any, project: Any, params: ResearchIn) -> None:
    """Register uploaded files and pasted texts as project user sources.

    Contents arrive inline (text formats only); each is written to a temp
    file so the service's normal copy-and-register path applies. Individual
    failures are logged, never fatal to the run.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        staged: list[tuple[Path, str | None]] = []
        for f in params.user_files:
            staged.append((Path(td) / _safe_source_filename(f.name), f.name))
            staged[-1][0].write_text(f.content, encoding="utf-8")
        for i, text in enumerate(params.user_texts, start=1):
            path = Path(td) / f"note-{i}.md"
            path.write_text(text, encoding="utf-8")
            staged.append((path, None))
        for path, title in staged:
            try:
                run_svc.add_user_source(project, str(path), title=title)
            except Exception:
                logger.warning("Skipping user document %r", path.name)


def _web_dist() -> Path | None:
    """Locate the built frontend, if any (env override, then ./web/dist)."""
    override = os.getenv("RESEARCHKIT_WEB_DIST")
    candidate = Path(override) if override else Path.cwd() / "web" / "dist"
    return candidate if (candidate / "index.html").is_file() else None


def create_app(service: SocialResearchService | None = None) -> FastAPI:
    """Build the FastAPI app (service injectable for tests)."""
    # Provider API keys from ./.env, like the CLI. Lives here (not main())
    # so custom ASGI deployments of create_app() get keys too.
    load_dotenv()
    app = FastAPI(title="researchkit", version=__version__, docs_url="/api/docs")
    app.add_middleware(
        CORSMiddleware,
        # Same-origin in production; localhost origins cover `vite dev`.
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    svc = service or SocialResearchService()
    runs: OrderedDict[str, _RunState] = OrderedDict()
    runs_lock = threading.Lock()

    # Optional bearer-token auth (RESEARCHKIT_AUTH_TOKEN). The API can start
    # paid provider work and read stored reports, so anything beyond loopback
    # must set this (enforced in main()).
    auth_token = os.getenv("RESEARCHKIT_AUTH_TOKEN")
    if auth_token:

        @app.middleware("http")
        async def _require_token(request: Request, call_next: Any) -> Any:
            if (
                request.url.path.startswith("/api/")
                and request.url.path != "/api/health"
            ):
                supplied = request.headers.get("Authorization", "")
                if not secrets.compare_digest(supplied, f"Bearer {auth_token}"):
                    return JSONResponse({"detail": "Unauthorized"}, status_code=401)
            return await call_next(request)

    def _register_run(run_id: str, state: _RunState) -> None:
        with runs_lock:
            if (
                sum(1 for s in runs.values() if s.status == "running")
                >= _MAX_ACTIVE_RUNS
            ):
                raise HTTPException(
                    status_code=429,
                    detail=f"Too many active runs (max {_MAX_ACTIVE_RUNS})",
                )
            runs[run_id] = state
            # Evict oldest *finished* runs only; active runs stay reachable.
            finished = [rid for rid, s in runs.items() if s.status != "running"]
            while len(runs) > _MAX_TRACKED_RUNS and finished:
                runs.pop(finished.pop(0), None)

    def _get_run(run_id: str) -> _RunState:
        with runs_lock:
            state = runs.get(run_id)
        if state is None:
            raise HTTPException(status_code=404, detail="Unknown run id")
        return state

    def _execute(state: _RunState, params: ResearchIn) -> None:
        try:
            # Fresh service per run unless one was injected (tests): isolates
            # concurrent runs from any shared-state assumptions in the engine.
            run_svc = svc if service is not None else SocialResearchService()
            providers = list(params.providers or _known_providers())
            if params.boost:
                boosted = run_svc.create_and_run_boosted(
                    params.topic,
                    days=params.days,
                    providers=providers,
                    sources=params.sources,
                    include_raw=params.include_raw,
                    preset_name=params.preset,
                    site_research_enabled=params.site_research,
                    site_research_sites=params.site_research_sites,
                    force_boost=True,
                    progress=state.events.put,
                )
                state.project = boosted.project.name
            elif params.user_urls or params.user_files or params.user_texts:
                project = run_svc.create_project(
                    topic=params.topic,
                    keywords=params.keywords,
                    days=params.days,
                    providers=providers,
                    sources=params.sources,
                    include_raw=params.include_raw,
                    preset_name=params.preset,
                    site_research_enabled=params.site_research,
                    site_research_sites=params.site_research_sites,
                )
                for url in params.user_urls:
                    try:
                        run_svc.add_user_source(project, url)
                    except Exception:
                        logger.warning("Skipping invalid user URL %r", url)
                _attach_document_sources(run_svc, project, params)
                run_svc.run_project(project, progress=state.events.put)
                state.project = project.name
            else:
                project, _artifacts = run_svc.create_and_run_project(
                    topic=params.topic,
                    keywords=params.keywords,
                    days=params.days,
                    providers=providers,
                    sources=params.sources,
                    include_raw=params.include_raw,
                    preset_name=params.preset,
                    site_research_enabled=params.site_research,
                    site_research_sites=params.site_research_sites,
                    progress=state.events.put,
                )
                state.project = project.name
            state.status = "done"
        except Exception as e:  # surface any pipeline failure to the client
            logger.exception("Research run failed")
            state.error = str(e)[:500]
            state.status = "error"
        finally:
            state.events.put(None)  # terminal sentinel

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/config")
    def config() -> dict[str, Any]:
        from researchkit.plugins import get_registry

        manager = svc.config_manager
        registry = get_registry()
        providers = list(_known_providers())
        return {
            "active_preset": manager.get_active_preset(),
            "presets": manager.get_preset_names(),
            "providers": providers,
            # UI default mirrors the legacy Gradio app: everything selected.
            "default_providers": providers,
            "connectors": list(_known_connectors()),
            "default_sites": list(_known_connectors()),
            "plugins": registry.plugin_versions(),
        }

    @app.post("/api/research", status_code=202)
    def start_research(params: ResearchIn) -> dict[str, str]:
        unknown = set(params.providers or []) - set(_known_providers())
        if unknown:
            raise HTTPException(
                status_code=422, detail=f"Unknown providers: {sorted(unknown)}"
            )
        unknown_sites = set(params.site_research_sites or []) - set(_known_connectors())
        if unknown_sites:
            raise HTTPException(
                status_code=422, detail=f"Unknown sites: {sorted(unknown_sites)}"
            )
        if invalid_sources := set(params.sources) - {"social", "web"}:
            raise HTTPException(
                status_code=422, detail=f"Unknown sources: {sorted(invalid_sources)}"
            )
        if params.providers is not None:
            params.providers = list(dict.fromkeys(params.providers))
        run_id = uuid.uuid4().hex
        state = _RunState()
        _register_run(run_id, state)
        threading.Thread(
            target=_execute,
            args=(state, params),
            name=f"research-{run_id[:8]}",
            daemon=True,
        ).start()
        return {"run_id": run_id}

    @app.get("/api/research/{run_id}")
    def run_status(run_id: str) -> RunStatus:
        state = _get_run(run_id)
        return RunStatus(status=state.status, project=state.project, error=state.error)

    @app.get("/api/research/{run_id}/events")
    def run_events(run_id: str) -> StreamingResponse:
        state = _get_run(run_id)

        def stream() -> Iterator[str]:
            while True:
                try:
                    event = state.events.get(timeout=_SSE_HEARTBEAT_SECONDS)
                except queue.Empty:
                    # Reconnect safety: a second/reconnected subscriber finds
                    # the queue already drained — emit terminal state instead
                    # of heartbeating forever.
                    if state.status != "running":
                        break
                    yield ": keep-alive\n\n"
                    continue
                if event is None:
                    break
                payload = json.dumps(event, default=str)
                yield f"event: progress\ndata: {payload}\n\n"
            if state.status == "done":
                done = json.dumps({"project": state.project})
                yield f"event: done\ndata: {done}\n\n"
            else:
                err = json.dumps({"message": state.error or "run failed"})
                yield f"event: error\ndata: {err}\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def _find_project(name: str) -> Project:
        for project in list_projects(PROJECTS_DIR):
            if project.name == name:
                return project
        raise HTTPException(status_code=404, detail="Unknown project")

    def _project_text(name: str, filename: str, media_type: str) -> PlainTextResponse:
        path = _find_project(name).path / filename
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"No {filename} yet")
        return PlainTextResponse(
            path.read_text(encoding="utf-8"), media_type=media_type
        )

    @app.get("/api/projects")
    def projects() -> list[dict[str, Any]]:
        return [p.summary() for p in list_projects(PROJECTS_DIR)]

    @app.get("/api/projects/{name}/report")
    def project_report(name: str) -> PlainTextResponse:
        return _project_text(name, "report.md", "text/markdown")

    @app.get("/api/projects/{name}/log")
    def project_log(name: str) -> PlainTextResponse:
        return _project_text(name, "run.log", "text/plain")

    @app.get("/api/projects/{name}/result")
    def project_result(name: str) -> JSONResponse:
        path = _find_project(name).path / "result.json"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="No result yet")
        return JSONResponse(json.loads(path.read_text(encoding="utf-8")))

    @app.get("/api/projects/{name}/prompt")
    def project_prompt(name: str) -> PlainTextResponse:
        from researchkit.article_prompt import build_article_prompt

        project = _find_project(name)
        if not project.report_path.is_file():
            raise HTTPException(status_code=404, detail="Run the project first")
        return PlainTextResponse(build_article_prompt(project))

    @app.get("/api/projects/{name}/links")
    def project_links(name: str, mode: str = "loose") -> JSONResponse:
        from researchkit.link_analytics import analyze_project_links

        if mode not in ("strict", "loose"):
            raise HTTPException(status_code=422, detail="mode must be strict|loose")
        path = _find_project(name).path / "result.json"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="No result yet")
        result = json.loads(path.read_text(encoding="utf-8"))
        return JSONResponse(analyze_project_links(result, mode=mode))  # type: ignore[arg-type]

    @app.post("/api/improve-topic")
    def improve_topic(params: TopicIn) -> dict[str, str]:
        from researchkit.prompt_improver import PromptImprover

        return {"topic": PromptImprover().improve_topic(params.topic)}

    @app.post("/api/keywords")
    def generate_keywords(params: TopicIn) -> dict[str, list[str]]:
        from researchkit.prompt_improver import PromptImprover

        return {
            "keywords": PromptImprover().generate_keywords(
                params.topic, count=params.count
            )
        }

    @app.post("/api/config/preset")
    def set_preset(params: PresetIn) -> dict[str, Any]:
        manager = svc.config_manager
        if params.preset not in manager.get_preset_names():
            raise HTTPException(status_code=422, detail="Unknown preset")
        manager.set_active_preset(params.preset)
        return {
            "active_preset": manager.get_active_preset(),
            "presets": manager.get_preset_names(),
        }

    dist = _web_dist()
    if dist is not None:
        app.mount("/", StaticFiles(directory=dist, html=True), name="web")
    else:  # pragma: no cover - trivial fallback branch
        logger.info("No web/dist build found; serving API only")

        @app.get("/")
        def index() -> dict[str, str]:
            return {
                "service": "researchkit",
                "docs": "/api/docs",
                "hint": "build the web UI with: cd web && npm run build",
            }

    return app


def main() -> None:
    """Entry point for the ``researchkit-server`` console script."""
    import uvicorn

    host = os.getenv("RESEARCHKIT_HOST", "127.0.0.1")
    port = int(os.getenv("RESEARCHKIT_PORT", "8000"))
    loopback = host in {"127.0.0.1", "::1", "localhost"}
    if not loopback and not os.getenv("RESEARCHKIT_AUTH_TOKEN"):
        raise SystemExit(
            "Refusing to bind researchkit-server to a non-loopback host "
            "without auth: the API starts paid provider runs and serves "
            "stored reports. Set RESEARCHKIT_AUTH_TOKEN (clients send "
            "'Authorization: Bearer <token>') or keep RESEARCHKIT_HOST on "
            "127.0.0.1 behind a reverse proxy that handles auth."
        )
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    main()
