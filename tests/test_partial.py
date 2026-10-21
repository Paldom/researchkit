"""A CLI killed at its deadline keeps what it already wrote (PLAN 3.1)."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

import pytest

from researchkit.providers.base import CliText, ProviderResult, salvage_timeout
from researchkit.safe_io import run_subprocess


def test_timeout_keeps_output_written_before_the_kill() -> None:
    script = "import sys, time; print('half an answer', flush=True); time.sleep(30)"
    with pytest.raises(subprocess.TimeoutExpired) as info:
        run_subprocess([sys.executable, "-c", script], timeout=1)
    assert "half an answer" in info.value.output


def _codex_line(item: dict[str, Any]) -> str:
    return json.dumps({"type": "item.completed", "item": item})


def _codex_timeout(lines: list[str]) -> subprocess.TimeoutExpired:
    exc = subprocess.TimeoutExpired("codex", 1800)
    exc.output = "\n".join(lines)
    return exc


def test_salvage_prefers_text_then_opened_pages_then_nothing() -> None:
    from researchkit.providers.codex_provider import CodexProvider

    parse = CodexProvider._parse_jsonl
    page = {
        "type": "web_search",
        "action": {"type": "other"},
        "query": "https://a.io/x",
    }
    msg = {"type": "agent_message", "text": "draft findings"}

    got = salvage_timeout(_codex_timeout([_codex_line(page), _codex_line(msg)]), parse)
    assert got is not None and isinstance(got[0], CliText) and got[0].partial
    assert got[0] == "draft findings" and got[1] == ["https://a.io/x"]

    got = salvage_timeout(_codex_timeout([_codex_line(page)]), parse)
    assert got is not None and "https://a.io/x" in got[0]

    assert salvage_timeout(_codex_timeout([]), parse) is None


def test_codex_timeout_becomes_a_partial_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    import researchkit.providers.codex_provider as mod

    monkeypatch.setattr(mod.shutil, "which", lambda b: f"/bin/{b}")
    p = mod.CodexProvider(model="gpt-6.1-sol", sources=["web"])
    msg = _codex_line({"type": "agent_message", "text": "found https://a.io/x"})

    def deadline(*_: Any, **__: Any) -> Any:
        raise _codex_timeout([msg])

    monkeypatch.setattr(mod, "run_subprocess", deadline)
    result = p.fetch_insights("topic", 7)
    assert result.is_success and result.meta["partial_branches"] == ["web"]
    assert result.slot_state == "partial"


def test_slot_state_partial_only_on_success() -> None:
    ok = ProviderResult("codex", "m", "text", meta={"partial": True})
    failed = ProviderResult("codex", "m", "", meta={"partial": True}, error="boom")
    assert ok.slot_state == "partial"
    assert failed.slot_state != "partial"


def test_partial_slot_counts_as_cited_but_degrades_the_run() -> None:
    from researchkit.providers.base import Source
    from researchkit.service import run_status

    partial = ProviderResult(
        "codex", "m", "text", [Source(url="https://a.io")], meta={"partial": True}
    )
    status = run_status([partial], "article", None)
    assert status["status"] == "degraded" and status["with_sources"] == 1
    assert status["reasons"] == ["cut off at the deadline: codex"]


def test_depth_telemetry_from_the_codex_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """PLAN 3.7: searches, pages read, cited-not-read, stop reason per branch."""
    import researchkit.providers.codex_provider as mod

    monkeypatch.setattr(mod.shutil, "which", lambda b: f"/bin/{b}")
    p = mod.CodexProvider(model="gpt-6.1-sol", sources=["web"])
    stream = "\n".join(
        [
            _codex_line({"type": "web_search", "action": {"type": "search"}}),
            _codex_line(
                {
                    "type": "web_search",
                    "action": {"type": "other"},
                    "query": "https://a.io/x",
                }
            ),
            _codex_line(
                {
                    "type": "agent_message",
                    "text": "see https://a.io/x and https://b.io/y",
                }
            ),
        ]
    )
    monkeypatch.setattr(
        mod,
        "run_subprocess",
        lambda *a, **k: subprocess.CompletedProcess("codex", 0, stream, ""),
    )
    depth = p.fetch_insights("topic", 7).meta["depth"]["web"]
    assert depth == {
        "searches": 1,
        "pages_read": 1,
        "messages": 1,
        "stop_reason": "complete",
        "cited_not_read": 1,
    }


def test_claude_depth_from_the_json_envelope() -> None:
    from researchkit.providers.claude_provider import ClaudeProvider

    envelope = {
        "result": "text",
        "num_turns": 7,
        "subtype": "success",
        "usage": {"server_tool_use": {"web_search_requests": 4}},
    }
    _, meta = ClaudeProvider()._parse_cli_json(json.dumps(envelope))
    assert meta["depth"] == {"turns": 7, "searches": 4, "stop_reason": "success"}
