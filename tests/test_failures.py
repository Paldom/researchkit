"""Failure classification and the exhausted-quota registry (PLAN 1.1, 1.2).

The fixture strings are real error texts mined from 248 past runs
(.local/research-202610/runs/topic-00-past-runs.md)."""

from __future__ import annotations

import pytest

from researchkit.failures import (
    FailureKind,
    QuotaExhaustedError,
    classify_failure,
    exhausted_reason,
    mark_exhausted,
    note_failure,
    pool_for_spec,
)
from researchkit.providers.base import ProviderResult

REAL_ERRORS = [
    ("You've hit your session limit · resets 12am (Europe/Budapest)", "quota"),
    ("You've hit your weekly limit · resets Mon", "quota"),
    (
        "kimi CLI failed (exit 1): error: failed to run prompt: provider.auth_error: "
        "403 You've reached your usage limit for this period",
        "quota",
    ),
    (
        '"message": "API error (status 402 Payment Required): Grok Build usage '
        'balance exhausted"',
        "quota",
    ),
    (
        "agy failed (exit 3): error: Individual quota reached. Please upgrade your "
        "subscription to increase your limits. Resets in 152h51m37s.",
        "quota",
    ),
    ("Error code: 429 - {'error': {'message': 'You have no credits'}}", "quota"),
    ("Not logged in · Please run /login", "auth"),
    ("websocket: HTTP error: 401 Unauthorized, url: wss://chatgpt.com/", "auth"),
    ("Error: --effort/--reasoning-effort must be one of: xhigh, high", "config"),
    (
        '{"subtype":"error_max_budget_usd","errors":["Reached maximum budget ($3)"]}',
        "config",
    ),
    ("Codex exec error: Command [...] timed out after 1800 seconds", "timeout"),
    ("Error code: 429 - Too Many Requests", "rate_limit"),
    ("API Error: Can't reach the API server — check your internet", "transient"),
    ("something unexpected", "error"),
]


@pytest.mark.parametrize(("message", "kind"), REAL_ERRORS)
def test_real_errors_classify(message: str, kind: str) -> None:
    assert classify_failure(message) == kind


def test_no_error_is_none() -> None:
    assert classify_failure(None) is None
    assert classify_failure("") is None


def test_pools() -> None:
    assert pool_for_spec("claude:claude-opus-5-5@xhigh") == "claude"
    assert pool_for_spec("deep:claude-opus-5-5") == "claude"
    assert pool_for_spec("codex:gpt-6.1-sol") == "codex"
    assert pool_for_spec("agy:gemini-3.8-flash-high") == "agy"
    assert pool_for_spec("grokcli:grok-4.7") == "grokcli"
    assert pool_for_spec("kimicli:kimi-code/k3") == "kimicli"
    assert pool_for_spec("gpt-5.4-mini") == "openai-api"
    assert pool_for_spec("gemini-3.8-flash") == "gemini-api"
    assert pool_for_spec("grok-4.6") == "grok-api"
    assert pool_for_spec("sonar") == "perplexity-api"


def test_registry_ttl_from_reset_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    import researchkit.failures as f

    now = [1000.0]
    monkeypatch.setattr(f.time, "monotonic", lambda: now[0])
    mark_exhausted("agy", "Individual quota reached. Resets in 0h2m0s.")
    assert exhausted_reason("agy")
    now[0] += 119
    assert exhausted_reason("agy")
    now[0] += 2
    assert exhausted_reason("agy") is None


def test_note_failure_marks_only_quota() -> None:
    assert note_failure("claude", "Reached maximum budget ($3)") == FailureKind.CONFIG
    assert exhausted_reason("claude") is None
    assert note_failure("claude", "You've hit your session limit") == FailureKind.QUOTA
    assert exhausted_reason("claude")


def test_complete_via_spec_refuses_exhausted_pool() -> None:
    from researchkit.council import complete_via_spec

    mark_exhausted("codex", "usage limit")
    with pytest.raises(QuotaExhaustedError):
        complete_via_spec("codex:gpt-6.1-sol", "", "hi", label="t")


def test_complete_via_spec_records_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    import researchkit.council as council

    def boom(*a: object, **k: object) -> str:
        raise RuntimeError("You've hit your session limit · resets 10pm")

    monkeypatch.setattr(council, "_complete_via_spec", boom)
    with pytest.raises(RuntimeError):
        council.complete_via_spec("claude:claude-opus-5-5", "", "hi", label="t")
    assert exhausted_reason("claude")


def test_slot_state() -> None:
    from researchkit.providers.base import Source

    ok = ProviderResult("x", "m", "text", sources=[Source(url="https://a.b")])
    assert ok.slot_state == "complete"
    assert ProviderResult("x", "m", "text").slot_state == "empty"
    assert (
        ProviderResult("x", "m", "", error="timed out after 1800").slot_state
        == "timeout"
    )
    assert ProviderResult("x", "m", "", error="usage limit").slot_state == "quota"


def test_retry_skips_quota_auth_config() -> None:
    from researchkit.network_retry import _is_transient_error

    assert not _is_transient_error(RuntimeError("Error code: 429 insufficient_quota"))
    assert not _is_transient_error(RuntimeError("Not logged in · Please run /login"))
    assert _is_transient_error(ConnectionError("connection reset"))


def test_writer_skips_exhausted_claude() -> None:
    from researchkit.final_summary import ArticleGenerator

    mark_exhausted("claude", "You've hit your session limit")
    gen = ArticleGenerator()
    assert gen._run_claude("prompt") is None
    assert gen.last_error and "quota exhausted" in gen.last_error


class TestRunStatus:
    """PLAN 1.3: the run reports what actually happened."""

    def _r(
        self, name: str, *, sources: int = 0, error: str | None = None
    ) -> ProviderResult:
        from researchkit.providers.base import Source

        return ProviderResult(
            name,
            "m",
            "" if error else "text",
            sources=[Source(url=f"https://{name}/{i}") for i in range(sources)],
            error=error,
        )

    def test_ok(self) -> None:
        from researchkit.service import run_status

        st = run_status([self._r("grok", sources=3), self._r("hackernews")], "article")
        assert st["status"] == "ok" and st["with_sources"] == 1
        assert st["slots"] == {"grok": "complete", "hackernews": "empty"}

    def test_degraded_on_failed_slot_or_missing_article(self) -> None:
        from researchkit.service import run_status

        st = run_status(
            [self._r("grok", sources=3), self._r("kimi", error="429 usage limit")],
            "article",
        )
        assert st["status"] == "degraded" and "kimi (quota)" in st["reasons"][0]
        assert run_status([self._r("grok", sources=1)], None)["status"] == "degraded"

    def test_failed_when_nothing_cited(self) -> None:
        from researchkit.service import run_status

        st = run_status([self._r("grok"), self._r("kimi", error="timed out")], "a")
        assert st["status"] == "failed"
        assert "no provider returned sources" in st["reasons"]
