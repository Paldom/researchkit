"""Classify provider/harness failures and remember exhausted quota pools.

Past runs failed mostly on subscription quota and timeouts, not on bad
prompts: Claude "session/weekly limit", Kimi "usage limit", Grok "balance
exhausted", Antigravity "Individual quota reached", Codex timeouts, API
"insufficient_quota". Retrying any of those spends wall time, and calling an
exhausted vendor again later in the same run only repeats the failure.

``classify_failure`` maps an error message to a :class:`FailureKind`.
``mark_exhausted`` / ``exhausted_reason`` keep a process-wide registry of
quota pools that are out. Subscription quota is account-wide, so a pool that
is out for one run is out for a concurrent run too. Entries expire (default
1 h, or the reset time an error states), so a long-lived server recovers on
its own.
"""

from __future__ import annotations

import re
import threading
import time
from enum import StrEnum


class FailureKind(StrEnum):
    """Why a provider or harness call produced no usable answer."""

    QUOTA = "quota"  # allowance / credit exhausted: stop using this pool
    RATE_LIMIT = "rate_limit"  # throttled: backing off can work
    AUTH = "auth"  # not logged in / bad key
    CONFIG = "config"  # bad flag, unknown model id
    TIMEOUT = "timeout"
    TRANSIENT = "transient"  # network hiccup
    ERROR = "error"  # anything else


# Order matters: quota wording wins over the HTTP status it often carries
# (Kimi reports a usage limit as "provider.auth_error: 403 …").
_PATTERNS: tuple[tuple[FailureKind, tuple[str, ...]], ...] = (
    (
        FailureKind.QUOTA,
        (
            "session limit",
            "weekly limit",
            "usage limit",
            "individual quota",
            "quota reached",
            "quota exhausted",
            "quota exceeded",
            "exceeded your current quota",
            "insufficient_quota",
            "you have no credits",
            "credit balance is too low",
            "balance exhausted",
            "usage balance",
            "payment required",
            "status 402",
            "error code: 402",
            "you have exhausted",
        ),
    ),
    (
        FailureKind.CONFIG,
        (
            "--effort",
            "--reasoning-effort",
            "unknown option",
            "unexpected argument",
            "unrecognized arguments",
            "invalid value for",
            "did not advertise that model",
            "model not found",
            "unknown model",
            "model_not_found",
            # A per-call budget cap, not account exhaustion: never mark the pool.
            "error_max_budget_usd",
            "reached maximum budget",
        ),
    ),
    (
        FailureKind.AUTH,
        (
            "not logged in",
            "please run /login",
            "401 unauthorized",
            "unauthorized",
            "invalid api key",
            "invalid_api_key",
            "authentication",
            "eligibility check",
            "oauth refresh lock",
            "not set (required",
            "_api_key not set",
        ),
    ),
    (
        FailureKind.TIMEOUT,
        ("timed out", "timeout expired", "timeoutexpired", "deadline exceeded"),
    ),
    (
        FailureKind.RATE_LIMIT,
        ("429", "rate limit", "rate_limit", "too many requests", "resource_exhausted"),
    ),
    (
        FailureKind.TRANSIENT,
        (
            "connection error",
            "connection reset",
            "connection refused",
            "can't reach the api server",
            "temporarily unavailable",
            "502 bad gateway",
            "503",
            "internal error",
        ),
    ),
)


def classify_failure(message: str | None) -> FailureKind | None:
    """Return the failure kind for an error message, or None for no error."""
    if not message:
        return None
    text = message.lower()
    for kind, needles in _PATTERNS:
        if any(n in text for n in needles):
            return kind
    return FailureKind.ERROR


# "Resets in 152h51m37s" (agy), "resets in 3h" — the only machine-readable
# reset hint the CLIs print. Absent one, the default TTL applies.
_RESETS_IN = re.compile(r"resets? in (?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?", re.I)
DEFAULT_EXHAUSTED_TTL_S = 3600.0
_MAX_TTL_S = 24 * 3600.0  # never park a pool longer than a day on one message


def _ttl_from(message: str) -> float:
    m = _RESETS_IN.search(message)
    if not m or not any(m.groups()):
        return DEFAULT_EXHAUSTED_TTL_S
    h, mi, s = (int(g) if g else 0 for g in m.groups())
    return float(min(max(h * 3600 + mi * 60 + s, 60), _MAX_TTL_S))


_lock = threading.Lock()
_exhausted: dict[str, tuple[float, str]] = {}  # pool -> (expires_at, reason)


def mark_exhausted(pool: str, reason: str, ttl_s: float | None = None) -> None:
    """Record that ``pool`` is out of quota until the TTL passes."""
    ttl = _ttl_from(reason) if ttl_s is None else ttl_s
    with _lock:
        _exhausted[pool] = (time.monotonic() + ttl, reason[:300])


def exhausted_reason(pool: str) -> str | None:
    """The reason ``pool`` is exhausted, or None when it is usable."""
    with _lock:
        entry = _exhausted.get(pool)
        if entry is None:
            return None
        expires, reason = entry
        if time.monotonic() >= expires:
            del _exhausted[pool]
            return None
        return reason


def clear_exhausted(pool: str | None = None) -> None:
    """Forget one pool (or all) — tests and an operator retry use this."""
    with _lock:
        if pool is None:
            _exhausted.clear()
        else:
            _exhausted.pop(pool, None)


def note_failure(pool: str, message: str | None) -> FailureKind | None:
    """Classify ``message`` and mark ``pool`` exhausted when it is quota."""
    kind = classify_failure(message)
    if kind is FailureKind.QUOTA and message:
        mark_exhausted(pool, message)
    return kind


def pool_for_spec(spec: str | None) -> str:
    """The quota pool a model spec bills: one per CLI harness or API vendor."""
    base = (spec or "").split("@", 1)[0].strip().lower()
    head = base.split(":", 1)[0]
    if head in ("claude", "deep", "deep-research") or base.startswith("claude"):
        return "claude"
    if head in ("codex", "agy", "antigravity", "grokcli", "kimicli"):
        return "agy" if head == "antigravity" else head
    for prefix, pool in (
        ("gpt", "openai-api"),
        ("o3", "openai-api"),
        ("o4", "openai-api"),
        ("gemini", "gemini-api"),
        ("models/gemini", "gemini-api"),
        ("grok", "grok-api"),
        ("glm", "glm-api"),
        ("kimi", "kimi-api"),
        ("moonshot", "kimi-api"),
        ("sonar", "perplexity-api"),
    ):
        if base.startswith(prefix):
            return pool
    return f"{base or 'unknown'}-api"


class QuotaExhaustedError(RuntimeError):
    """Raised instead of calling a pool already known to be out of quota."""

    def __init__(self, pool: str, reason: str) -> None:
        super().__init__(f"{pool} quota exhausted earlier in this process: {reason}")
        self.pool = pool
        self.reason = reason
