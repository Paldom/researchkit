"""Social and web run concurrently and independently (PLAN 1.8, 1.5)."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from researchkit.providers.base import run_branches


def test_branches_overlap_in_time() -> None:
    started: list[float] = []
    lock = threading.Lock()

    def slow() -> str:
        with lock:
            started.append(time.monotonic())
        time.sleep(0.2)
        return "ok"

    t0 = time.monotonic()
    out = run_branches({"social": slow, "web": slow})
    assert out == {"social": "ok", "web": "ok"}
    assert time.monotonic() - t0 < 0.35  # serial would be >= 0.4
    assert abs(started[0] - started[1]) < 0.1


def test_branch_failure_is_kept_not_raised() -> None:
    def boom() -> str:
        raise RuntimeError("web broke")

    out = run_branches({"social": lambda: "fine", "web": boom})
    assert out["social"] == "fine"
    assert isinstance(out["web"], RuntimeError)


def test_contextvars_reach_branches() -> None:
    import contextvars

    var: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="-")
    var.set("run-42")
    out = run_branches({"a": var.get, "b": var.get})
    assert out == {"a": "run-42", "b": "run-42"}


def test_codex_keeps_social_when_web_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    import researchkit.providers.codex_provider as mod

    monkeypatch.setattr(mod.shutil, "which", lambda b: f"/bin/{b}")
    p = mod.CodexProvider(model="gpt-6.1-sol")

    def fake(*, system_prompt: str, user_prompt: str, label: str) -> Any:
        if label.endswith(":web"):
            raise RuntimeError("timed out after 1800")
        return "social findings https://example.com/a", ["https://example.com/a"]

    monkeypatch.setattr(p, "_run_query", fake)
    result = p.fetch_insights("topic", 7)
    assert result.is_success
    assert "Social Media Analysis" in result.raw_text
    assert "timed out" in result.meta["branch_errors"]["web"]


def test_codex_fails_only_when_every_branch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import researchkit.providers.codex_provider as mod

    monkeypatch.setattr(mod.shutil, "which", lambda b: f"/bin/{b}")
    p = mod.CodexProvider(model="gpt-6.1-sol")

    def fake(**kw: Any) -> Any:
        raise RuntimeError("You've hit your usage limit")

    monkeypatch.setattr(p, "_run_query", fake)
    result = p.fetch_insights("topic", 7)
    assert not result.is_success and result.slot_state == "quota"
