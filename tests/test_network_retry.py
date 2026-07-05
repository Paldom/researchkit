"""Tests for the unified network retry policy."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import requests

from researchkit.network_retry import (
    DEFAULT_MAX_ATTEMPTS,
    RETRYABLE_STATUS_CODES,
    network_retry,
    with_network_retry,
    with_network_retry_async,
)


def _make_counter(side_effects: list[Any]) -> tuple[Callable[..., Any], list[int]]:
    """Build a callable that pops from side_effects each call.

    Side effects can be exceptions (raised) or any other value (returned).
    Returns (callable, call_log) where call_log is a list that grows with each
    invocation so tests can count calls.
    """
    call_log: list[int] = []

    def _call(*_: Any, **__: Any) -> Any:
        call_log.append(1)
        nxt = side_effects.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    return _call, call_log


# --- retryable exception types ---------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("boom"),
        httpx.TimeoutException("boom"),
        httpx.ReadError("boom"),
        httpx.RemoteProtocolError("boom"),
        requests.exceptions.ConnectionError("boom"),
        requests.exceptions.Timeout("boom"),
        requests.exceptions.ChunkedEncodingError("boom"),
        ConnectionResetError("boom"),
        TimeoutError("boom"),
    ],
)
def test_retries_on_transient_exception(exc: Exception) -> None:
    """All transient network exceptions should trigger a retry."""
    fn, calls = _make_counter([exc, exc, "ok"])
    result = with_network_retry(fn, label="test.transient")
    assert result == "ok"
    assert len(calls) == 3


def test_retries_on_oserror_dns_failure() -> None:
    """OSError (catches socket.gaierror, network unreachable) should retry."""
    err = OSError("Name or service not known")
    fn, calls = _make_counter([err, "ok"])
    result = with_network_retry(fn, label="test.oserror")
    assert result == "ok"
    assert len(calls) == 2


def test_does_not_retry_filenotfound() -> None:
    """FileNotFoundError is OSError-subclass but means missing binary — fail fast."""
    fn, calls = _make_counter([FileNotFoundError("missing")])
    with pytest.raises(FileNotFoundError):
        with_network_retry(fn, label="test.fnf")
    assert len(calls) == 1


def test_does_not_retry_on_value_error() -> None:
    """Programming errors (ValueError, TypeError) must not be retried."""
    fn, calls = _make_counter([ValueError("bad input")])
    with pytest.raises(ValueError):
        with_network_retry(fn, label="test.valueerror")
    assert len(calls) == 1


# --- retryable status codes -------------------------------------------------


class _FakeHttpError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeSdkError(Exception):
    """Mimics OpenAI/Anthropic SDK errors that expose .response.status_code."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.response = _FakeResponse(status_code)


@pytest.mark.parametrize("code", sorted(RETRYABLE_STATUS_CODES))
def test_retries_on_retryable_status_code(code: int) -> None:
    fn, calls = _make_counter([_FakeHttpError(code), "ok"])
    result = with_network_retry(fn, label="test.status")
    assert result == "ok"
    assert len(calls) == 2


@pytest.mark.parametrize("code", sorted(RETRYABLE_STATUS_CODES))
def test_retries_on_retryable_status_via_response(code: int) -> None:
    """SDK-style errors that nest the status under `.response.status_code`."""
    fn, calls = _make_counter([_FakeSdkError(code), "ok"])
    result = with_network_retry(fn, label="test.sdk")
    assert result == "ok"
    assert len(calls) == 2


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_does_not_retry_on_client_errors(code: int) -> None:
    """4xx (other than 408/425/429) should not be retried."""
    fn, calls = _make_counter([_FakeHttpError(code)])
    with pytest.raises(_FakeHttpError):
        with_network_retry(fn, label="test.4xx")
    assert len(calls) == 1


def _real_requests_http_error(status_code: int) -> requests.exceptions.HTTPError:
    resp = requests.Response()
    resp.status_code = status_code
    return requests.exceptions.HTTPError(f"{status_code} Client Error", response=resp)


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_real_requests_http_4xx_not_retried(code: int) -> None:
    """Regression for M13: a genuine requests.HTTPError subclasses OSError, so the
    blanket OSError branch must not treat a 4xx as transient. The _FakeHttpError
    above is a plain Exception and cannot exercise this ordering bug."""
    fn, calls = _make_counter([_real_requests_http_error(code)])
    with pytest.raises(requests.exceptions.HTTPError):
        with_network_retry(fn, label="test.real4xx")
    assert len(calls) == 1


@pytest.mark.parametrize("code", [429, 500, 503])
def test_real_requests_http_retryable_retried(code: int) -> None:
    """Retryable statuses on a real requests.HTTPError still retry."""
    fn, calls = _make_counter([_real_requests_http_error(code), "ok"])
    assert with_network_retry(fn, label="test.real5xx") == "ok"
    assert len(calls) == 2


# --- attempt budget ---------------------------------------------------------


def test_gives_up_after_max_attempts() -> None:
    """After DEFAULT_MAX_ATTEMPTS, the last exception is re-raised."""
    err = httpx.ConnectError("nope")
    fn, calls = _make_counter([err] * (DEFAULT_MAX_ATTEMPTS + 3))
    with pytest.raises(httpx.ConnectError):
        with_network_retry(fn, label="test.giveup")
    assert len(calls) == DEFAULT_MAX_ATTEMPTS


# --- before_sleep logging ---------------------------------------------------


def test_before_sleep_emits_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Each retry should emit a structured WARN with the retry metadata."""
    fn, _ = _make_counter([httpx.ConnectError("x"), "ok"])
    with caplog.at_level(logging.WARNING, logger="researchkit.network_retry"):
        result = with_network_retry(fn, label="test.log", provider="testprov")
    assert result == "ok"
    retry_records = [
        r for r in caplog.records if getattr(r, "stage", "") == "network_retry"
    ]
    assert len(retry_records) == 1
    record = retry_records[0]
    assert record.label == "test.log"
    assert record.provider == "testprov"
    assert record.attempt == 1
    assert record.error_type == "ConnectError"


# --- transient phrase detection (for SDKs without structured errors) -------


def test_retries_on_grpc_unavailable_phrase() -> None:
    """gRPC-style errors (xai-sdk) only carry a string message."""
    err = Exception("UNAVAILABLE: connection error")
    fn, calls = _make_counter([err, "ok"])
    result = with_network_retry(fn, label="test.grpc")
    assert result == "ok"
    assert len(calls) == 2


def test_retries_on_rate_limit_phrase() -> None:
    err = Exception("Rate limit exceeded, please try again later")
    fn, calls = _make_counter([err, "ok"])
    result = with_network_retry(fn, label="test.rl")
    assert result == "ok"
    assert len(calls) == 2


# --- decorator form ---------------------------------------------------------


def test_decorator_wraps_callable() -> None:
    call_log: list[int] = []

    @network_retry(label="test.decorator", provider="prov")
    def fetch() -> str:
        call_log.append(1)
        if len(call_log) < 2:
            raise httpx.ConnectError("once")
        return "ok"

    assert fetch() == "ok"
    assert len(call_log) == 2


# --- async form -------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_retry_succeeds_after_failure() -> None:
    call_log: list[int] = []

    async def _coro() -> str:
        call_log.append(1)
        if len(call_log) < 3:
            raise httpx.ConnectError("flaky")
        return "ok"

    result = await with_network_retry_async(_coro, label="test.async")
    assert result == "ok"
    assert len(call_log) == 3


@pytest.mark.asyncio
async def test_async_retry_propagates_non_transient() -> None:
    async def _coro() -> str:
        raise ValueError("nope")

    with pytest.raises(ValueError):
        await with_network_retry_async(_coro, label="test.async_fast_fail")
