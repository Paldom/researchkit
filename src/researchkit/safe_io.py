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
    unchanged.
    """
    popen_kwargs: dict[str, object] = {
        "stdin": subprocess.PIPE if input is not None else subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": env,
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
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        with contextlib.suppress(Exception):
            proc.communicate(timeout=5)  # reap so no zombie
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
        "ANTHROPIC_API_KEY",
        "TAVILY_API_KEY",
        "EXA_API_KEY",
        "YOUTUBE_API_KEY",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "RAPIDAPI_KEY",
        "X_RAPIDAPI_KEY",
    }
)


def scrubbed_env(*, keep: frozenset[str] = frozenset()) -> dict[str, str]:
    """Return a copy of ``os.environ`` with known provider secrets removed.

    ``keep`` retains the calling CLI's own auth key (e.g. Codex may run on
    ``OPENAI_API_KEY``; agy keeps ``GEMINI_API_KEY``/``GOOGLE_API_KEY``). Everything
    else the CLI relies on (PATH, HOME, XDG/config dirs) is preserved.
    """
    remove = _SECRET_ENV_KEYS - keep
    return {k: v for k, v in os.environ.items() if k not in remove}


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


def safe_fetch_text(
    url: str,
    *,
    timeout: float = 8.0,
    max_bytes: int = 100_000,
    max_redirects: int = 3,
    headers: dict[str, str] | None = None,
) -> tuple[str | None, str]:
    """Best-effort SSRF-guarded GET. Returns ``(text_or_None, final_url)``.

    Refuses non-http(s) schemes and any host that resolves to a loopback/
    link-local/private/reserved address, following redirects manually and
    re-validating each hop. Caps response size and time, and ignores proxy env
    (``trust_env=False``). Returns ``(None, url)`` on any refusal/failure — callers
    treat that as "no title".

    Residual: a perfectly-timed DNS-rebind (TTL-0 host that resolves public here
    then private inside requests) is not fully closed by pre-resolution; pinning
    the socket to the validated IP would break TLS SNI, so for this best-effort
    title-fetch of already-cited public URLs we accept that narrow residual.
    """
    session = requests.Session()
    session.trust_env = False
    current = url
    try:
        for _ in range(max_redirects + 1):
            try:
                parts = urlsplit(current)
            except ValueError:
                return None, url
            if parts.scheme not in ("http", "https") or not parts.hostname:
                return None, url
            if not _host_is_public(parts.hostname):
                logger.debug(
                    "safe_fetch_text: refusing non-public host %s", parts.hostname
                )
                return None, url
            try:
                resp = session.get(
                    current,
                    timeout=timeout,
                    allow_redirects=False,
                    stream=True,
                    headers=headers or {},
                )
            except requests.RequestException:
                return None, url
            try:
                if resp.status_code in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("Location")
                    if not loc:
                        return None, current
                    current = urljoin(current, loc)
                    continue
                if resp.status_code != 200:
                    return None, current
                total = 0
                chunks: list[bytes] = []
                for chunk in resp.iter_content(8192):
                    if not chunk:
                        continue
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= max_bytes:
                        break
                raw = b"".join(chunks)[:max_bytes]
                return raw.decode(resp.encoding or "utf-8", errors="replace"), current
            finally:
                resp.close()
        return None, url
    finally:
        session.close()


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
