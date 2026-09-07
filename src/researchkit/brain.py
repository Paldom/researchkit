"""Per-project brainkit brain: built from materials/, browsed by the web UI.

The compounding brain (``--ingest``) is the user's long-lived memory across
runs; this one is the run's own, written to ``<project>/brain/`` so the pack
ships with a queryable, navigable form of itself.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from researchkit.project import Project

logger = logging.getLogger(__name__)

_RESERVED = {"index.md", "log.md"}
_META_LINE = re.compile(r'^(title|type|url):\s*"?(.*?)"?\s*$')


def build_brain(project: Project) -> Path | None:
    """Ingest the project's result.json + materials/ into ``<project>/brain/``.

    brainkit is a soft dependency: without it (or without results) there is no
    brain, and the run is not failed for it, since the report already exists.
    Idempotent: re-running after ``materials --refresh`` rewrites the notes. A
    boosted parent ingests its ``subprojects/`` recursively, so one call on the
    parent yields the brain over the whole study.
    """
    if not project.has_results:
        return None
    try:
        from brainkit.brain import ingest_research_project
    except ImportError:
        logger.info("brainkit not installed; skipping the per-project brain")
        return None
    try:
        report = ingest_research_project(
            project.path.resolve(), project.brain_dir.resolve()
        )
    except Exception as e:  # a brain is a bonus, never the run's failure
        logger.warning(f"Brain build failed for {project.name}: {e}")
        return None
    logger.info(
        f"Brain built: {len(report.source_notes)} source note(s), "
        f"{len(report.report_notes)} report note(s) in {project.brain_dir}",
        extra={"stage": "brain_done"},
    )
    return project.brain_dir


def brain_notes(brain_dir: Path) -> list[dict[str, Any]]:
    """Every note in the brain (brain-relative path, slug, title, type, url).

    Frontmatter is read with a regex rather than by importing brainkit, so a
    brain stays browsable on a machine without brainkit installed.
    """
    notes_dir = brain_dir / "notes"
    if not notes_dir.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    for path in sorted(notes_dir.rglob("*.md")):
        if path.name in _RESERVED:
            continue
        meta = _frontmatter(path)
        entries.append(
            {
                "path": path.relative_to(brain_dir).as_posix(),
                "slug": path.stem,
                "title": meta.get("title") or path.stem,
                "type": meta.get("type") or path.parent.name,
                "url": meta.get("url", ""),
            }
        )
    return entries


def _frontmatter(path: Path) -> dict[str, str]:
    meta: dict[str, str] = {}
    with path.open(encoding="utf-8") as fh:
        if fh.readline().strip() != "---":
            return meta
        for line in fh:
            if line.strip() == "---":
                break
            m = _META_LINE.match(line.strip())
            if m:
                meta[m.group(1)] = m.group(2)
    return meta


def read_brain_file(brain_dir: Path, relative: str) -> str | None:
    """A markdown file inside the brain by brain-relative path.

    ``None`` when the path escapes the brain, is not markdown, or is missing:
    the web API exposes this, so the brain directory is the trust boundary.
    """
    root = brain_dir.resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root) or target.suffix != ".md":
        return None
    if not target.is_file():
        return None
    return target.read_text(encoding="utf-8")
