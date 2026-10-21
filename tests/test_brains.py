"""Shared brains: many runs, one brain (PLAN §4b)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("brainkit.brain")

from researchkit import brains


def _run(root: Path, name: str, url: str, text: str) -> Path:
    d = root / name
    (d / "materials").mkdir(parents=True)
    (d / "materials" / "001-a.md").write_text(
        f"---\nurl: {url}\ntitle: Page {name}\ncontent_digest: x\n---\n\n{text}\n"
    )
    (d / "materials" / "index.json").write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "url": url,
                        "status": "fetched",
                        "file": "001-a.md",
                        "origin": "http",
                    }
                ],
                "fetched": 1,
                "total_cited": 1,
            }
        )
    )
    (d / "result.json").write_text(
        json.dumps(
            {
                "topic": f"topic {name}",
                "days": 7,
                "meta_summary": f"## Findings\n{text}",
                "provider_results": [
                    {
                        "provider": "grok",
                        "model": "m",
                        "raw_text": text,
                        "sources": [{"url": url}],
                    }
                ],
            }
        )
    )
    return d


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(brains.BRAINS_DIR_ENV, str(tmp_path / "brains"))
    return tmp_path


def test_create_list_and_feed_two_runs(root: Path) -> None:
    brain = brains.create_brain("team")
    assert (brain / "index.md").is_file()
    a = _run(
        root,
        "20261021_000001_a",
        "https://a.example/x",
        "Prompt injection containment holds.",
    )
    b = _run(
        root,
        "20261021_000002_b",
        "https://b.example/y",
        "Kimi headless runs shell commands.",
    )
    brains.ingest_into(brain, a)
    brains.ingest_into(brain, b)
    again = brains.ingest_into(brain, a)  # re-adding is a no-op
    assert again["pruned"] == 0
    [info] = brains.list_brains()
    assert info.name == "team" and info.runs == 2 and info.notes >= 2


def test_prior_knowledge_returns_citable_sources(root: Path) -> None:
    brain = brains.create_brain("team")
    brains.ingest_into(
        brain,
        _run(
            root,
            "20261021_000003_c",
            "https://c.example/z",
            "Kimi headless runs shell commands by default.",
        ),
    )
    hits = brains.prior_knowledge(brain, "kimi headless shell")
    assert hits and hits[0].url == "https://c.example/z"
    assert brains.prior_knowledge(root / "missing", "x") == []


def test_names_and_paths(root: Path) -> None:
    assert brains.resolve_brain("team") == brains.brains_root() / "team"
    assert brains.resolve_brain("./local-brain").name == "local-brain"
    with pytest.raises(ValueError):
        brains.resolve_brain("Bad Name!")


def test_prior_sources_are_numbered_and_marked(root: Path) -> None:
    from researchkit.evidence import load_evidence

    ev = load_evidence(
        None, {}, prior=[("https://c.example/z", "Old page", "known text")]
    )
    [src] = ev.sources
    assert src.status == "brain" and src.archived and "(earlier research)" in src.title
    assert ev.passages[0].text == "known text"
