"""Every article gets /deslop and /illustrate (owner rule, 2026-10-26)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from researchkit import article_finish as af

ARTICLE = (
    "# Caching\n\nIt's not just a setting, it's a shift [1]. Live costs 3x more [2].\n"
)


@pytest.fixture
def skills_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(af, "missing_skills", lambda: [])
    monkeypatch.setattr(af, "_update_result", [None])  # already updated


def _fake_run(on_deslop: str | None, svg: bool = True) -> Any:
    calls: list[list[str]] = []

    def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        work = Path(kw["cwd"])
        prompt = cmd[2]
        if prompt.startswith("/deslop") and on_deslop is not None:
            (work / "article.md").write_text(on_deslop, encoding="utf-8")
        if prompt.startswith("/illustrate") and svg:
            out = work / "diagram-design" / "caching"
            out.mkdir(parents=True)
            (out / "caching.svg").write_text("<svg/>", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, '{"result": "ok"}', "")

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_deslop_and_illustration_land_in_the_article(
    tmp_path: Path, skills_present: None
) -> None:
    clean = "# Caching\n\nThe setting changes research [1]. Live costs 3x more [2].\n"
    run = _fake_run(clean)
    done = af.finish_article(
        ARTICLE,
        title="Caching",
        project_dir=tmp_path,
        model="m",
        max_budget=1.0,
        run=run,
    )
    assert done.deslopped and done.notes == []
    assert done.illustrations == ["article-assets/caching.svg"]
    assert (tmp_path / "article-assets" / "caching.svg").is_file()
    assert done.markdown.splitlines()[:3] == [
        "# Caching",
        "",
        "![Caching](article-assets/caching.svg)",
    ]
    cmd = run.calls[0]
    assert "--strict-mcp-config" in cmd and "bypassPermissions" not in cmd
    assert "WebSearch,WebFetch" in cmd


def test_a_deslop_that_drops_a_citation_is_rejected(
    tmp_path: Path, skills_present: None
) -> None:
    lossy = "# Caching\n\nThe setting changes research. Live costs 3x more [2].\n"
    done = af.finish_article(
        ARTICLE,
        title="Caching",
        project_dir=tmp_path,
        model="m",
        max_budget=1.0,
        run=_fake_run(lossy, svg=False),
    )
    assert not done.deslopped and done.markdown == ARTICLE
    assert any("original kept" in n for n in done.notes)
    assert "illustrate produced no SVG" in done.notes


def test_missing_skills_skip_the_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(af, "_update_result", [None])
    monkeypatch.setattr(af, "missing_skills", lambda: ["deslop"])
    done = af.finish_article(
        ARTICLE,
        title="t",
        project_dir=tmp_path,
        model="m",
        max_budget=1.0,
        run=_fake_run(None),
    )
    assert done.markdown == ARTICLE and "skills not installed" in done.notes[0]


def test_skills_update_runs_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(af, "_update_result", [])
    calls: list[list[str]] = []

    def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    assert af.update_skills(run) is None
    assert af.update_skills(run) is None
    assert len(calls) == 1 and calls[0][:5] == [
        "npx",
        "--yes",
        "skills",
        "update",
        "-g",
    ]


def test_off_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEARCHKIT_ARTICLE_FINISH", "0")
    assert not af.enabled()
    monkeypatch.setenv("RESEARCHKIT_ARTICLE_FINISH", "1")
    assert af.enabled()
