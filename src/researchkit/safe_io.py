"""Shared, unit-tested I/O safety primitives.

These consolidate several patterns the review flagged as repeated-and-unsafe
across the codebase:

- :func:`run_subprocess` — run a CLI in its own process group and kill the whole
  group (not just the direct child) on timeout/cancellation, so a `claude`/`codex`/
  `agy` grandchild can't be orphaned and keep spending. Decodes output as UTF-8
  with ``errors="replace"`` so non-Latin research text never raises on a non-UTF-8
  locale. (Review: C2, L26.)
- :func:`safe_fetch_text` — best-effort HTTP GET with an SSRF guard: refuse
  non-public hosts, follow redirects manually re-validating each hop, cap bytes +
  time, and ignore proxy env. (Review: S2; also hardens the Gemini redirect
  resolution in M6.)
- :func:`atomic_write_text` — write via a temp file + ``os.replace`` + ``fsync`` so a
  crash mid-write can't corrupt config/result files. (Review: L18.)
- :func:`safe_join_within` / :func:`safe_unlink_within` — reject path-traversal
  filenames before touching disk. (Review: S1.)
- :func:`extract_urls_balanced` — extract URLs keeping balanced parens (Wikipedia
  ``_(genus)`` links) while trimming trailing punctuation. Shared so every provider
  extracts URLs the same way. (Review: S3.)
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Subprocess: own process group, kill-the-group on timeout, UTF-8 decoding
# ---------------------------------------------------------------------------


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Kill the whole process group of ``proc`` (best effort, cross-platform)."""
    try:
        if sys.platform == "win32":
            # CREATE_NEW_PROCESS_GROUP was set; signal then hard-kill.
            proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            proc.kill()
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, OSError):
        with contextlib.suppress(OSError):
            proc.kill()


def run_subprocess(
    cmd: list[str],
    *,
    input: str | None = None,
    timeout: float,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``cmd`` in its own session/process group and return a CompletedProcess.

    Unlike :func:`subprocess.run`, on :class:`subprocess.TimeoutExpired` (or any
    interruption) the ENTIRE process group is killed, so CLI-spawned grandchildren
    (node/MCP/subagents) can't be orphaned and keep running/spending. Output is
    always captured and decoded as UTF-8 with ``errors="replace"``.

    ``TimeoutExpired`` is re-raised after cleanup so existing callers' handling is
    unchanged; its ``output``/``stderr`` hold everything written before the kill.

    Every child is stamped with ``NESTED_ENV_VAR`` (see below) so an agent CLI we
    launch cannot turn around and start a researchkit run. Stamping here rather
    than at the entrypoints makes that hold however researchkit was started —
    console script, ``uvicorn ... --factory``, MCP stdio, or embedded as a library.
    """
    child_env = dict(os.environ if env is None else env)
    child_env[NESTED_ENV_VAR] = str(os.getpid())
    popen_kwargs: dict[str, object] = {
        "stdin": subprocess.PIPE if input is not None else subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": child_env,
        "cwd": cwd,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)  # type: ignore[call-overload]
    try:
        out, err = proc.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        _kill_process_group(proc)
        with contextlib.suppress(Exception):
            # Reap (no zombie) and keep what the CLI wrote before the deadline:
            # streaming CLIs (codex --json, kimi stream-json) leave a usable
            # partial answer that callers salvage from ``e.output`` (PLAN 3.1).
            e.output, e.stderr = proc.communicate(timeout=5)
        raise
    except BaseException:
        _kill_process_group(proc)
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


# ---------------------------------------------------------------------------
# Environment scrubbing for CLI subprocesses
# ---------------------------------------------------------------------------

# Known secret env vars that a research CLI (codex/agy) shouldn't need but which,
# left in place, are an exfiltration payload if untrusted web content steers the
# agent. (Review M7.)
_SECRET_ENV_KEYS = frozenset(
    {
        "OPENAI_API_KEY",
        "XAI_API_KEY",
        "PERPLEXITY_API_KEY",
        "PERPLEXITYAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "ZAI_API_KEY",
        "Z_AI_API_KEY",
        "GLM_API_KEY",
        "ZHIPUAI_API_KEY",
        "MOONSHOT_API_KEY",
        "KIMI_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "TAVILY_API_KEY",
        "EXA_API_KEY",
        "BRAVE_API_KEY",
        "YOUTUBE_API_KEY",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "RAPIDAPI_KEY",
        "X_RAPIDAPI_KEY",
    }
)


# researchkit NEVER invokes itself: `explore`/`--boost` fan out in-process. So a
# researchkit entrypoint reached from inside a researchkit-spawned agent CLI can
# only be that agent deciding, on its own, to shell out to us — and each run it
# starts spawns more agents that do the same. That is exponential, not a fan-out:
# one topic produced ~200 runs in 45 minutes before this guard existed.
#
# `run_subprocess` stamps this into EVERY child env it builds, so the block does
# not depend on which env builder a provider used (`scrubbed_env` here, or the
# `{**os.environ}` copies in claude_provider/council/final_summary) — nor on
# researchkit having been started through one of its console scripts. The value
# is the stamping process's PID, so only a genuinely separate process is refused.
NESTED_ENV_VAR = "RESEARCHKIT_NESTED"

_NESTED_ERROR = (
    "Refusing to run: RESEARCHKIT_NESTED names another process, so this one is "
    "a descendant of a coding-agent CLI that researchkit itself started.\n"
    "researchkit never calls itself — boosted runs fan out in-process — so this "
    "is an agent shelling out to researchkit on its own initiative, which "
    "recurses exponentially.\n"
    "Do not retry, and do not work around this: research the topic with your "
    "own web search and report what you find."
)
# Deliberately no copy-pasteable bypass here. The reader of this message is
# usually the very agent that just tried to recurse, and it will follow a
# printed command far more readily than it will reason about an env var.


def guard_no_nested_run() -> str | None:
    """Refuse re-entry from a researchkit-spawned agent CLI; else claim the marker.

    Returns an error message for the caller to print before exiting non-zero, or
    ``None`` when the run may proceed — in which case the marker is set so every
    agent CLI this process spawns inherits it.

    The marker holds the PID that claimed it, not a flag: a *child* sees a
    foreign PID and is refused, while re-entering an entrypoint inside the same
    process (tests, embedding researchkit as a library) is not.
    """
    claimed_by = os.environ.get(NESTED_ENV_VAR)
    mine = str(os.getpid())
    if claimed_by and claimed_by != mine:
        return _NESTED_ERROR
    os.environ[NESTED_ENV_VAR] = mine
    return None


def scrubbed_env(*, keep: frozenset[str] = frozenset()) -> dict[str, str]:
    """Return a copy of ``os.environ`` with known provider secrets removed.

    ``keep`` retains the calling CLI's own auth key (e.g. Codex may run on
    ``OPENAI_API_KEY``; agy keeps ``GEMINI_API_KEY``/``GOOGLE_API_KEY``). Everything
    else the CLI relies on (PATH, HOME, XDG/config dirs) is preserved.
    """
    remove = _SECRET_ENV_KEYS - keep
    return {k: v for k, v in os.environ.items() if k not in remove}


# Env vars that move Claude Code off the logged-in claude.ai subscription onto
# API-key, gateway or cloud billing. researchkit runs Claude only as a harness,
# so every `claude` subprocess drops them; CLAUDE_CODE_OAUTH_TOKEN (a
# subscription token from `claude setup-token`) is deliberately kept.
CLAUDE_API_ROUTE_ENV_KEYS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_MANTLE",
    }
)


def claude_subscription_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Env for a `claude` subprocess that can only bill the subscription.

    Copies ``base`` (default ``os.environ``) without
    :data:`CLAUDE_API_ROUTE_ENV_KEYS` and turns off Claude Code's
    non-essential traffic.
    """
    source = os.environ if base is None else base
    env = {k: v for k, v in source.items() if k not in CLAUDE_API_ROUTE_ENV_KEYS}
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return env


# ---------------------------------------------------------------------------
# SSRF-guarded fetch
# ---------------------------------------------------------------------------


def _ip_is_public(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _host_is_public(host: str) -> bool:
    """True only if EVERY DNS-resolved address for ``host`` is a public IP."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    if not infos:
        return False
    return all(_ip_is_public(info[4][0]) for info in infos)


@dataclass(frozen=True)
class FetchResult:
    """What one guarded GET returned, so callers can classify, not just guess.

    ``text`` is the decoded body (None when nothing usable came back);
    ``body`` the raw bytes (PDF parsing needs them); ``status`` the final
    HTTP status (0 when no response); ``content_type`` the MIME type without
    parameters; ``truncated`` True when ``max_bytes`` cut the body; ``error``
    a short reason when the fetch was refused or failed.
    """

    url: str
    final_url: str
    text: str | None = None
    body: bytes = b""
    status: int = 0
    content_type: str = ""
    truncated: bool = False
    error: str = ""


def safe_fetch(
    url: str,
    *,
    timeout: float = 8.0,
    max_bytes: int = 100_000,
    max_redirects: int = 3,
    headers: dict[str, str] | None = None,
) -> FetchResult:
    """SSRF-guarded GET that reports status, MIME type, truncation and errors.

    Refuses non-http(s) schemes and any host that resolves to a loopback/
    link-local/private/reserved address, following redirects manually and
    re-validating each hop. Caps response size and time, and ignores proxy env
    (``trust_env=False``).

    Residual: a perfectly-timed DNS-rebind (TTL-0 host that resolves public here
    then private inside requests) is not fully closed by pre-resolution; pinning
    the socket to the validated IP would break TLS SNI, so for this best-effort
    fetch of already-cited public URLs we accept that narrow residual.
    """
    session = requests.Session()
    session.trust_env = False
    current = url
    try:
        for _ in range(max_redirects + 1):
            try:
                parts = urlsplit(current)
            except ValueError:
                return FetchResult(url, url, error="malformed url")
            if parts.scheme not in ("http", "https") or not parts.hostname:
                return FetchResult(url, url, error="unsupported scheme")
            if not _host_is_public(parts.hostname):
                logger.debug("safe_fetch: refusing non-public host %s", parts.hostname)
                return FetchResult(url, url, error="non-public host")
            try:
                resp = session.get(
                    current,
                    timeout=timeout,
                    allow_redirects=False,
                    stream=True,
                    headers=headers or {},
                )
            except requests.RequestException as e:
                return FetchResult(url, current, error=type(e).__name__)
            try:
                if resp.status_code in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("Location")
                    if not loc:
                        return FetchResult(
                            url,
                            current,
                            status=resp.status_code,
                            error="redirect without location",
                        )
                    current = urljoin(current, loc)
                    continue
                ctype = (
                    (resp.headers.get("Content-Type") or "")
                    .split(";")[0]
                    .strip()
                    .lower()
                )
                if resp.status_code != 200:
                    return FetchResult(
                        url,
                        current,
                        status=resp.status_code,
                        content_type=ctype,
                        error=f"http {resp.status_code}",
                    )
                total = 0
                chunks: list[bytes] = []
                truncated = False
                for chunk in resp.iter_content(8192):
                    if not chunk:
                        continue
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= max_bytes:
                        truncated = True
                        break
                raw = b"".join(chunks)[:max_bytes]
                return FetchResult(
                    url,
                    current,
                    text=raw.decode(resp.encoding or "utf-8", errors="replace"),
                    body=raw,
                    status=resp.status_code,
                    content_type=ctype,
                    truncated=truncated,
                )
            finally:
                resp.close()
        return FetchResult(url, url, error="too many redirects")
    finally:
        session.close()


def safe_fetch_text(
    url: str,
    *,
    timeout: float = 8.0,
    max_bytes: int = 100_000,
    max_redirects: int = 3,
    headers: dict[str, str] | None = None,
) -> tuple[str | None, str]:
    """Best-effort SSRF-guarded GET. Returns ``(text_or_None, final_url)``.

    Thin wrapper over :func:`safe_fetch` for callers that only need the text.
    """
    result = safe_fetch(
        url,
        timeout=timeout,
        max_bytes=max_bytes,
        max_redirects=max_redirects,
        headers=headers,
    )
    return result.text, result.final_url


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def atomic_write_text(path: str | Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` atomically (temp file + fsync + os.replace).

    A crash mid-write leaves the original file intact rather than truncated.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def _fsync_dir(directory: Path) -> None:
    """fsync a directory so the rename is durable (no-op where unsupported)."""
    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        dfd = os.open(str(directory), os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


# ---------------------------------------------------------------------------
# Path-traversal-safe join / unlink
# ---------------------------------------------------------------------------


def safe_join_within(base: str | Path, name: str) -> Path:
    """Return ``base / name`` iff ``name`` is a plain basename that stays inside ``base``.

    Rejects absolute paths, empty names, and any name containing directory
    separators or ``..`` — raising :class:`ValueError`. Never uses ``assert`` (which
    ``python -O`` strips) and never *truncates* (``Path(name).name`` would silently
    turn ``../x`` into ``x``).
    """
    if not name or name != Path(name).name:
        raise ValueError(f"unsafe filename (not a basename): {name!r}")
    base_resolved = Path(base).resolve()
    candidate = (base_resolved / name).resolve()
    if candidate != base_resolved and base_resolved not in candidate.parents:
        raise ValueError(f"filename escapes base directory: {name!r}")
    return candidate


def safe_unlink_within(base: str | Path, name: str) -> bool:
    """Delete ``base/name`` if it is a safe basename and an existing file.

    Returns True if a file was deleted, False otherwise. Raises ValueError on an
    unsafe (traversal) name so a tampered config surfaces rather than deleting an
    out-of-tree file.
    """
    candidate = safe_join_within(base, name)
    if candidate.exists() and candidate.is_file():
        candidate.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# Balanced-paren URL extraction (shared by providers)
# ---------------------------------------------------------------------------

_URL_SCAN_RE = re.compile(r'https?://[^\s<>\[\]{}"\'`]+')


def extract_urls_balanced(text: str) -> list[str]:
    """Extract URLs from free text, deduped and order-preserved.

    Keeps parens balanced *within* the URL (``.../GPT-5_(model)``) while trimming
    trailing markdown emphasis, punctuation, and a closing paren that belongs to
    the surrounding prose. Matches the logic in ``ClaudeProvider._extract_urls`` so
    every provider extracts URLs identically.
    """
    urls: list[str] = []
    for raw in _URL_SCAN_RE.findall(text or ""):
        url = raw
        while True:
            stripped = url.rstrip(".,;:!?\"'`*_")
            if stripped.endswith(")") and stripped.count("(") < stripped.count(")"):
                stripped = stripped[:-1]
            if stripped == url:
                break
            url = stripped
        if url:
            urls.append(url)
    return list(dict.fromkeys(urls))
