"""Per-project brain: built from a run's pack, browsable without brainkit."""

from __future__ import annotations

import json
import sys
import types
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from researchkit.brain import brain_notes, build_brain, read_brain_file
from researchkit.project import Project, ProjectConfig


def _project(tmp_path: Path, *, with_results: bool = True) -> Project:
    folder = tmp_path / "20260705_test_topic"
    folder.mkdir(parents=True)
    if with_results:
        payload = {
            "topic": "test topic",
            "meta_summary": "## Summary\n\nA finding.",
            "provider_results": [],
        }
        (folder / "result.json").write_text(json.dumps(payload), encoding="utf-8")
        materials = folder / "materials"
        materials.mkdir()
        (materials / "001-a.md").write_text(
            '---\ntitle: "A page"\nurl: "https://a.test/page"\n---\n\n'
            "Page body about the finding.\n",
            encoding="utf-8",
        )
    return Project(
        path=folder,
        config=ProjectConfig(topic="test topic"),
        created_at=datetime(2026, 7, 5),
    )


def _fake_brainkit(monkeypatch: pytest.MonkeyPatch, *, fail: bool = False) -> None:
    """A stand-in brainkit that writes the layout the browser expects."""

    def ingest(project_dir: Path, brain_dir: Path, **_: Any) -> Any:
        if fail:
            raise RuntimeError("boom")
        topics = brain_dir / "notes" / "topics"
        sources = brain_dir / "notes" / "sources"
        topics.mkdir(parents=True)
        sources.mkdir(parents=True)
        (brain_dir / "index.md").write_text(
            "# Brain index\n\n* [test topic](notes/topics/t1.md)\n", encoding="utf-8"
        )
        (topics / "index.md").write_text("# topics\n", encoding="utf-8")
        (topics / "t1.md").write_text(
            '---\ntitle: "test topic"\ntype: "topic"\n---\n\nsee [[s1]]\n',
            encoding="utf-8",
        )
        (sources / "s1.md").write_text(
            '---\ntitle: "A page"\ntype: "source"\nurl: "https://a.test/page"\n---\n\nBody\n',
            encoding="utf-8",
        )
        return types.SimpleNamespace(source_notes=[sources / "s1.md"], report_notes=[])

    pkg = types.ModuleType("brainkit")
    mod = types.ModuleType("brainkit.brain")
    mod.ingest_research_project = ingest  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "brainkit", pkg)
    monkeypatch.setitem(sys.modules, "brainkit.brain", mod)


def test_build_brain_writes_a_browsable_brain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_brainkit(monkeypatch)
    project = _project(tmp_path)

    assert build_brain(project) == project.brain_dir
    assert project.summary()["has_brain"] is True

    notes = brain_notes(project.brain_dir)
    # index.md files are listings, not notes; frontmatter gives title/type/url.
    assert [n["path"] for n in notes] == ["notes/sources/s1.md", "notes/topics/t1.md"]
    assert notes[0] == {
        "path": "notes/sources/s1.md",
        "slug": "s1",
        "title": "A page",
        "type": "source",
        "url": "https://a.test/page",
    }
    assert read_brain_file(project.brain_dir, "index.md") is not None
    assert read_brain_file(project.brain_dir, "notes/topics/index.md") == "# topics\n"


def test_build_brain_is_a_bonus_never_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No results: nothing to ingest.
    assert build_brain(_project(tmp_path, with_results=False)) is None
    # brainkit missing: skipped quietly.
    monkeypatch.setitem(sys.modules, "brainkit", None)
    monkeypatch.setitem(sys.modules, "brainkit.brain", None)
    project = _project(tmp_path / "b")
    assert build_brain(project) is None
    # brainkit raising: logged, not raised.
    _fake_brainkit(monkeypatch, fail=True)
    assert build_brain(project) is None
    assert project.summary()["has_brain"] is False


def test_read_brain_file_stays_inside_the_brain(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    (brain / "notes").mkdir(parents=True)
    (tmp_path / "secret.md").write_text("nope", encoding="utf-8")
    (brain / "notes" / "n.txt").write_text("x", encoding="utf-8")
    (brain / "notes" / "plain.md").write_text("no frontmatter\n", encoding="utf-8")

    assert read_brain_file(brain, "../secret.md") is None
    assert read_brain_file(brain, "notes/n.txt") is None
    assert read_brain_file(brain, "missing.md") is None
    assert brain_notes(tmp_path / "nobrain") == []
    # A note without frontmatter still lists, by filename and directory.
    assert brain_notes(brain) == [
        {
            "path": "notes/plain.md",
            "slug": "plain",
            "title": "plain",
            "type": "notes",
            "url": "",
        }
    ]


def test_real_brainkit_ingests_the_pack(tmp_path: Path) -> None:
    """Integration: the real brainkit, when installed, yields topic + source notes."""
    pytest.importorskip("brainkit.brain")
    project = _project(tmp_path)
    assert build_brain(project) == project.brain_dir
    types_seen = {n["type"] for n in brain_notes(project.brain_dir)}
    assert {"topic", "source"} <= types_seen
