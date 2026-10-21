"""PLAN 4.6: a syndicated copy is one origin, not a second source."""

from __future__ import annotations

import json
from pathlib import Path

from researchkit.citations import check_publication, renumber
from researchkit.evidence import load_evidence

_STORY = " ".join(
    f"word{i} reported fact number {i} in the original story." for i in range(40)
)
_OTHER = " ".join(
    f"unrelated{i} measured benchmark {i} on a different system." for i in range(40)
)


def _project(tmp_path: Path) -> Path:
    mats = tmp_path / "materials"
    mats.mkdir()
    entries = []
    for i, (url, body) in enumerate(
        [
            ("https://news.example.com/story", _STORY),
            ("https://syndicator.example.net/copied-story", _STORY),
            ("https://lab.example.org/benchmark", _OTHER),
        ],
        start=1,
    ):
        (mats / f"s{i}.md").write_text(body, encoding="utf-8")
        entries.append({"url": url, "status": "fetched", "file": f"s{i}.md"})
    (mats / "index.json").write_text(json.dumps({"entries": entries}), encoding="utf-8")
    return tmp_path


def test_syndicated_copy_is_folded_into_its_original(tmp_path: Path) -> None:
    evidence = load_evidence(_project(tmp_path), {})
    by_n = evidence.by_n()
    assert by_n[2].copy_of == 1 and by_n[2].origin == "news.example.com"
    assert by_n[3].copy_of == 0 and by_n[3].origin == "lab.example.org"


def test_corroboration_by_a_copy_fails_the_gate(tmp_path: Path) -> None:
    evidence = load_evidence(_project(tmp_path), {})
    copy = "The result is corroborated by two outlets [1][2]."
    independent = "The result is corroborated by two outlets [1][3]."
    for text, flagged in ((copy, True), (independent, False)):
        cited = renumber([text], evidence)
        issues = check_publication({"report": cited.texts[0]}, cited, {})
        kinds = {i.kind for i in issues if i.hard}
        assert ("single_origin_corroboration" in kinds) is flagged
