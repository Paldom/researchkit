"""Shared brains: one brainkit brain fed by many research runs (PLAN §4b).

A brain is a first-class, named target, independent of any run. Create one
in advance (``researchkit brain create``), point any number of runs at it
(``--brain <name>``, the web UI's brain picker, the API and MCP), attach
earlier runs later (``researchkit brain add``), and let a new run start from
what the brain already knows (:func:`prior_knowledge`).

brainkit already does the hard part: ingest is idempotent, a source cited by
several runs gets one note with a ``corroboration`` count, and a single-writer
OS lock guards the tree. Its lock raises instead of waiting, and is
re-entrant within a process, so this module serializes ingests per brain
in-process and retries across processes with a bounded backoff.
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BRAINS_DIR_ENV = "RESEARCHKIT_BRAINS_DIR"
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_INGEST_WAIT_S = 600.0  # another process ingesting: wait this long at most
_locks: dict[Path, threading.Lock] = {}
_locks_guard = threading.Lock()


class BrainkitMissingError(RuntimeError):
    """brainkit is an optional dependency; shared brains need it installed."""


def _brainkit() -> Any:
    try:
        import brainkit.brain as brainkit_brain
    except ImportError as e:
        raise BrainkitMissingError(
            "shared brains need brainkit in this environment "
            "(uv pip install -e ../brainkit, or pip install brainkit)"
        ) from e
    return brainkit_brain


def brains_root() -> Path:
    """Where named brains live: ``$RESEARCHKIT_BRAINS_DIR`` or ``~/.researchkit/brains``."""
    override = os.environ.get(BRAINS_DIR_ENV)
    return (
        Path(override).expanduser()
        if override
        else Path.home() / ".researchkit" / "brains"
    )


def resolve_brain(ref: str) -> Path:
    """A brain name (``team-notes``) or an explicit path (``./brain``, ``~/b``)."""
    ref = ref.strip()
    if not ref:
        raise ValueError("empty brain reference")
    if "/" in ref or ref.startswith((".", "~")):
        return Path(ref).expanduser().resolve()
    if not _NAME.match(ref):
        raise ValueError(
            f"invalid brain name {ref!r}: lowercase letters, digits, '.', '_' or '-' "
            "(or pass a path)"
        )
    return brains_root() / ref


def create_brain(ref: str) -> Path:
    """Create an empty brain ready for ingest and queries (idempotent)."""
    path = resolve_brain(ref)
    path.mkdir(parents=True, exist_ok=True)
    _brainkit().build_index(path)
    return path


@dataclass(frozen=True)
class BrainInfo:
    """One brain in the registry, as listed by the CLI, API and UI."""

    name: str
    path: Path
    notes: int
    runs: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "notes": self.notes,
            "runs": self.runs,
        }


def _runs_ingested(brain: Path) -> int:
    """Distinct research runs in brainkit's journal (``* **Ingest**: `<run>` — …``)."""
    log = brain / "log.md"
    if not log.is_file():
        return 0
    return len(set(re.findall(r"\*\*Ingest\*\*: `([^`]+)`", log.read_text("utf-8"))))


def describe(path: Path, name: str | None = None) -> BrainInfo:
    notes = (
        sum(1 for _ in (path / "notes").rglob("*.md"))
        if (path / "notes").is_dir()
        else 0
    )
    return BrainInfo(
        name=name or path.name, path=path, notes=notes, runs=_runs_ingested(path)
    )


def list_brains() -> list[BrainInfo]:
    """Every named brain under :func:`brains_root`, by name."""
    root = brains_root()
    if not root.is_dir():
        return []
    return [
        describe(p)
        for p in sorted(root.iterdir())
        if p.is_dir() and (p / "index.md").is_file()
    ]


def _lock_for(brain: Path) -> threading.Lock:
    key = brain.resolve()
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _count(value: Any) -> int:
    """brainkit reports counts as ints or as lists, depending on the field."""
    if isinstance(value, int):
        return value
    try:
        return len(value)
    except TypeError:
        return 0


def ingest_into(brain: Path, project_dir: Path) -> dict[str, Any]:
    """Ingest one finished project into ``brain``, safely under concurrency.

    Serialized per brain inside this process; across processes, brainkit's
    ``WriterBusyError`` is retried with backoff for up to ten minutes.
    Re-ingesting the same run is a no-op (brainkit guarantee).
    """
    bk = _brainkit()
    brain.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _INGEST_WAIT_S
    delay = 1.0
    with _lock_for(brain):
        while True:
            try:
                report = bk.ingest_research_project(project_dir.resolve(), brain)
                break
            except bk.WriterBusyError:
                if time.monotonic() + delay > deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
    return {
        "project": project_dir.name,
        "brain": str(brain),
        "sources": _count(getattr(report, "source_notes", 0)),
        "skipped": _count(getattr(report, "skipped_sources", 0)),
        "pruned": _count(getattr(report, "pruned_sources", 0)),
    }


@dataclass(frozen=True)
class PriorHit:
    """Something the brain already knows that bears on a new run's topic."""

    url: str
    title: str
    snippet: str
    published: str = ""


def prior_knowledge(brain: Path, topic: str, *, limit: int = 8) -> list[PriorHit]:
    """Source notes the brain already holds for ``topic`` (PLAN B.4).

    Only cited source notes come back (they carry a URL, so the new report
    can cite them); topic/report notes are summaries, and digest 11 says to
    seed from raw sources, never from distilled memory. An empty or missing
    brain returns nothing.
    """
    if not (brain / "index.md").is_file():
        return []
    try:
        hits, _coverage = _brainkit().context(
            brain, topic, limit=limit * 2, kind="source"
        )
    except (BrainkitMissingError, OSError, ValueError):
        return []
    out: list[PriorHit] = []
    for hit in hits:
        if hit.url and len(out) < limit:
            out.append(PriorHit(hit.url, hit.title, hit.snippet, hit.published))
    return out
