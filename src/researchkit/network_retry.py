"""Unified retry/reconnect policy for all network calls.

Every outbound HTTP/SDK/subprocess call in this app should be wrapped with one
of the helpers exported from this module so the app shrugs off transient
network failures (dropped packets, DNS hiccups, flaky Wi-Fi, congested APIs)
instead of failing the whole run on the first error.

Policy (aggressive — covers tethering, VPN drops, slow Wi-Fi):
- 5 attempts, capped at ~120s total wall time
- Exponential backoff (1s, 2s, 4s, 8s, 16s) with jitter up to 2s
- Retry only on transient errors: connection/timeout exceptions or
  HTTP 408/425/429/500/502/503/504. Non-transient (4xx auth/validation,
  ValueError, etc.) fails fast.
- Each retry is logged at WARN with structured fields so it lands in
  the per-run log and surfaces in the CLI/web UI.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import subprocess
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, TypeVar

import httpx
import requests
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    stop_after_delay,
    wait_exponential,
    wait_random,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Retry budget. The per-request HTTP timeout is ~180s (provider_http_timeout),
# so the total deadline must exceed a couple of attempts or slow-but-recoverable
# calls would get ZERO retries (review M4). 3 attempts with a 600s ceiling lets a
# stalled call retry ~twice while capping a hung provider at ~10 min instead of
# the old 15-min worst case of 5 unbounded attempts.
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MAX_DELAY_SECONDS = 600.0
DEFAULT_BACKOFF_MIN = 1.0
DEFAULT_BACKOFF_MAX = 30.0
DEFAULT_JITTER_MAX = 2.0

RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})

# Exception types that always indicate a transient network problem.
_TRANSIENT_EXCEPTION_TYPES: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.NetworkError,
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
    socket.timeout,
    socket.gaierror,
    ConnectionError,
    ConnectionResetError,
    TimeoutError,
)


def _extract_status_code(exc: BaseException) -> int | None:
    """Best-effort extraction of an HTTP status code from common SDK errors."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status

    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status
        status = getattr(response, "status", None)
        if isinstance(status, int):
            return status

    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code

    return None


def _is_transient_error(exc: BaseException) -> bool:
    """Decide whether an exception is worth retrying.

    Two paths to True:
    1. The exception is (or wraps) a known transient network error.
    2. The exception carries a retryable HTTP status code.
    """
    if isinstance(exc, _TRANSIENT_EXCEPTION_TYPES):
        return True

    # HTTP status must be decided BEFORE the blanket OSError branch below:
    # requests.exceptions.HTTPError subclasses OSError, so a real 401/403/404
    # from a requests-based connector would otherwise be treated as transient
    # and retried pointlessly (review M13). A non-retryable client error is
    # terminal; a retryable status short-circuits to True.
    status = _extract_status_code(exc)
    if status is not None:
        if status in RETRYABLE_STATUS_CODES:
            return True
        if 400 <= status < 500:
            return False

    # OSError covers DNS / 'Network is unreachable' / EPIPE; treat as transient.
    # Exclude FileNotFoundError (missing CLI) and PermissionError.
    if isinstance(exc, OSError) and not isinstance(
        exc,
        FileNotFoundError | PermissionError | IsADirectoryError | NotADirectoryError,
    ):
        return True

    # subprocess.TimeoutExpired counts as transient for the subprocess wrappers.
    if isinstance(exc, subprocess.TimeoutExpired):
        return True

    # Fall through: inspect the exception message for telltale patterns from
    # SDKs that don't surface structured error info (e.g. xai-sdk wraps gRPC).
    msg = str(exc).lower()
    transient_phrases = (
        "connection reset",
        "connection aborted",
        "connection refused",
        "connection error",
        "temporary failure",
        "timed out",
        "timeout",
        "rate limit",
        "rate-limit",
        "too many requests",
        "service unavailable",
        "bad gateway",
        "gateway timeout",
        "remote disconnected",
        "broken pipe",
        "network is unreachable",
        "name or service not known",
        "eof occurred",
        "ssl handshake",
        "unavailable",  # gRPC UNAVAILABLE
        "deadline exceeded",  # gRPC DEADLINE_EXCEEDED
    )
    return any(phrase in msg for phrase in transient_phrases)


def _redact_url_secrets(text: str) -> str:
    """Redact credential query params (e.g. ``key=...`` API keys) in URLs."""
    return re.sub(r"\b(key|api_key|apikey|token)=[^&\s]+", r"\1=REDACTED", text)


def _log_before_sleep(
    label: str, provider: str | None
) -> Callable[[RetryCallState], None]:
    """Build a tenacity ``before_sleep`` callback that emits a structured WARN."""

    def _before_sleep(state: RetryCallState) -> None:
        exc = state.outcome.exception() if state.outcome else None
        if exc is None:
            return
        next_sleep = getattr(state.next_action, "sleep", None)
        wait_s = float(next_sleep) if next_sleep is not None else 0.0
        attempt = state.attempt_number
        # Friendly, user-facing message — also rendered in CLI/web log view.
        logger.warning(
            "Retrying %s (attempt %d/%d, waiting %.1fs) — %s: %s",
            label,
            attempt,
            DEFAULT_MAX_ATTEMPTS,
            wait_s,
            type(exc).__name__,
            _redact_url_secrets(str(exc))[:200],
            extra={
                "stage": "network_retry",
                "label": label,
                "attempt": attempt,
                "max_attempts": DEFAULT_MAX_ATTEMPTS,
                "wait_s": round(wait_s, 2),
                "error_type": type(exc).__name__,
                "provider": provider or "",
            },
        )

    return _before_sleep


def _build_retry_kwargs(label: str, provider: str | None) -> dict[str, Any]:
    """Common kwargs for both sync ``Retrying`` and ``AsyncRetrying``."""
    return {
        "stop": stop_after_attempt(DEFAULT_MAX_ATTEMPTS)
        | stop_after_delay(DEFAULT_MAX_DELAY_SECONDS),
        "wait": wait_exponential(
            multiplier=1, min=DEFAULT_BACKOFF_MIN, max=DEFAULT_BACKOFF_MAX
        )
        + wait_random(0, DEFAULT_JITTER_MAX),
        "retry": retry_if_exception(_is_transient_error),
        "before_sleep": _log_before_sleep(label, provider),
        "reraise": True,
    }


def with_network_retry(
    fn: Callable[..., T],
    *args: Any,
    label: str,
    provider: str | None = None,
    **kwargs: Any,
) -> T:
    """Run a synchronous callable under the unified retry policy.

    Example:
        result = with_network_retry(
            requests.get, url, timeout=(10, 60),
            label="github.search_repos", provider="github",
        )
    """
    for attempt in Retrying(**_build_retry_kwargs(label, provider)):
        with attempt:
            return fn(*args, **kwargs)
    raise RuntimeError(
        f"with_network_retry({label}) exited without returning"
    )  # unreachable


async def with_network_retry_async(
    coro_factory: Callable[[], Awaitable[T]],
    *,
    label: str,
    provider: str | None = None,
) -> T:
    """Run an async callable factory under the unified retry policy.

    Pass a factory (not a coroutine) so each attempt creates a fresh awaitable.

    Example:
        async def _call():
            return await client.fetch(...)
        result = await with_network_retry_async(_call, label="xyz", provider="x")
    """
    async for attempt in AsyncRetrying(**_build_retry_kwargs(label, provider)):
        with attempt:
            return await coro_factory()
    raise RuntimeError(
        f"with_network_retry_async({label}) exited without returning"
    )  # unreachable


def network_retry(
    label: str, provider: str | None = None
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator form of :func:`with_network_retry` for sync callables."""

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            return with_network_retry(
                fn, *args, label=label, provider=provider, **kwargs
            )

        return wrapper

    return decorator


def network_retry_async(
    label: str, provider: str | None = None
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Decorator form of :func:`with_network_retry_async` for coroutine functions."""

    def decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            return await with_network_retry_async(
                lambda: fn(*args, **kwargs), label=label, provider=provider
            )

        return wrapper

    return decorator


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_DELAY_SECONDS",
    "RETRYABLE_STATUS_CODES",
    "network_retry",
    "network_retry_async",
    "with_network_retry",
    "with_network_retry_async",
]


# `asyncio` is referenced lazily by tenacity AsyncRetrying internals; keep the
# import alive so tools like ruff don't strip it.
_ = asyncio
