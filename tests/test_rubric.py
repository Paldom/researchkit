"""PLAN 4.4: one rubric, rendered for the writer and scored by the judge."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from researchkit import rubric
from researchkit.prompts import get_report_body_system_prompt


def test_writer_prompt_carries_every_rubric_item() -> None:
    prompt = get_report_body_system_prompt()
    assert f"rubric v{rubric.RUBRIC_VERSION}" in prompt
    for _, text in rubric.RUBRIC:
        assert text in prompt


def test_missing_or_malformed_verdicts_fail_closed() -> None:
    answer = 'Sure: {"cited": {"pass": true, "why": "all cited"}, "specific": {"pass": "yes"}}'
    verdicts = rubric.parse_verdicts(answer)
    assert verdicts["cited"] == {"pass": True, "why": "all cited"}
    assert verdicts["specific"]["pass"] is False  # not a bool: no verdict
    assert verdicts["open_questions"]["why"] == "no verdict from the judge"
    assert set(verdicts) == {rid for rid, _ in rubric.RUBRIC}
    assert not any(v["pass"] for v in rubric.parse_verdicts("garbage").values())


def test_service_score_writes_rubric_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import researchkit.publish as publish_module
    from researchkit.service import SocialResearchService

    all_pass = {rid: {"pass": True, "why": "ok"} for rid, _ in rubric.RUBRIC}
    seen: dict[str, Any] = {}

    def fake_write(self: Any, prompt: str) -> str:
        seen["prompt"] = prompt
        return json.dumps(all_pass)

    monkeypatch.setattr(publish_module._Writer, "write", fake_write)
    svc = SocialResearchService(projects_dir=tmp_path)
    project = svc.create_project(topic="graded")
    project.report_path.write_text("# Report\n\nA fact [1].", encoding="utf-8")

    result = svc.score(project)
    assert result["passed"] == result["total"] == len(rubric.RUBRIC)
    assert "A fact [1]." in seen["prompt"]
    saved = json.loads((project.path / "rubric.json").read_text(encoding="utf-8"))
    assert saved["rubric_version"] == rubric.RUBRIC_VERSION and saved["model"]
