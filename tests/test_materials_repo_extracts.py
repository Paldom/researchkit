"""Repo extracts join the materials manifest as files inside materials/."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from researchkit.materials import download_materials
from researchkit.project import Project, ProjectConfig


def test_repo_extract_is_copied_into_materials_and_listed(tmp_path: Path) -> None:
    folder = tmp_path / "20260705_repo_topic"
    (folder / "repos").mkdir(parents=True)
    extract = "---\nslug: o-r\nurl: https://github.com/o/r\nkind: repo_extract\n---\n\n# o-r\n"
    (folder / "repos" / "o-r.md").write_text(extract, encoding="utf-8")
    (folder / "result.json").write_text(
        json.dumps(
            {
                "topic": "repo topic",
                "provider_results": [],
                "repo_extracts": [
                    {
                        "url": "https://github.com/o/r",
                        "slug": "o-r",
                        "extract_file": "o-r.md",
                    },
                    {
                        "url": "https://github.com/o/gone",
                        "slug": "gone",
                        "extract_file": "gone.md",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    project = Project(
        path=folder,
        config=ProjectConfig(topic="repo topic"),
        created_at=datetime(2026, 7, 5),
    )

    manifest = download_materials(project, limit=5)

    rows = [e for e in manifest["entries"] if e.get("origin") == "repo_extract"]
    # The missing extract is skipped; the present one is copied in, so the
    # manifest never names a file outside materials/ (brainkit fails closed).
    assert [r["file"] for r in rows] == ["repo-o-r.md"]
    assert rows[0]["status"] == "fetched"
    assert (folder / "materials" / "repo-o-r.md").read_text(encoding="utf-8") == extract
    assert manifest["repo_extracts"] == 1
