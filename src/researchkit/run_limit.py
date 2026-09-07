"""A cross-process ceiling on concurrent research runs.

The recursion guard in :mod:`researchkit.safe_io` closes one specific way runs
multiply (an agent researchkit spawned shelling back into researchkit). This
closes the general case: however runs get started — several terminals, the web
UI, a script in a loop, a future fan-out nobody has written yet — no more than
``RESEARCHKIT_MAX_CONCURRENT_RUNS`` of them collect from providers at once.

It has to work ACROSS processes: the storm that motivated this was ~200
separate ``researchkit`` processes, so a semaphore inside one interpreter would
have counted one run and let the other 199 through.

The mechanism is a slot directory. Acquiring means atomically creating
``slot-<i>.json`` with ``O_EXCL``, which the filesystem serializes for us — two
processes racing for the last slot cannot both win, no lockfile protocol
needed. A slot whose owning PID is gone is stale (kill -9, crash, power loss)
and is reclaimed by the next arrival, so a crash cannot permanently shrink the
pool.

Three behaviours, matching what a ceiling is for:

- **alert**  — a warning (and a progress event) once the pool is nearly full,
  before anything is refused.
- **pause**  — a run that finds no slot waits for one rather than dying, so a
  legitimate burst queues instead of failing.
- **degrade** — waiting past ``RESEARCHKIT_RUN_SLOT_WAIT_S`` raises
  :class:`TooManyConcurrentRuns`, which names what is holding the slots.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Ten concurrent runs is a lot of spending at once and still far below the
# ~4/minute the runaway storm sustained.
DEFAULT_MAX_CONCURRENT_RUNS = 10
DEFAULT_WAIT_S = 300.0
_POLL_S = 2.0

_ENV_MAX = "RESEARCHKIT_MAX_CONCURRENT_RUNS"
_ENV_WAIT = "RESEARCHKIT_RUN_SLOT_WAIT_S"
_ENV_DIR = "RESEARCHKIT_RUNS_DIR"


class TooManyConcurrentRuns(RuntimeError):
    """Raised when no slot frees up within the wait budget."""


def runs_dir() -> Path:
    """Directory holding the slot files.

    Under ``$HOME`` rather than the projects dir on purpose: the cap must hold
    across every researchkit process this user runs, and two runs pointed at
    different ``RESEARCHKIT_PROJECTS_DIR`` values still share one machine.
    """
    override = os.getenv(_ENV_DIR)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".researchkit" / "runs"


def max_concurrent_runs() -> int:
    """The ceiling; ``0`` (or a bad value) disables the cap entirely."""
    raw = os.getenv(_ENV_MAX)
    if raw is None:
        return DEFAULT_MAX_CONCURRENT_RUNS
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning(f"Ignoring invalid {_ENV_MAX}={raw!r}")
        return DEFAULT_MAX_CONCURRENT_RUNS


def _wait_budget_s() -> float:
    raw = os.getenv(_ENV_WAIT)
    if raw is None:
        return DEFAULT_WAIT_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning(f"Ignoring invalid {_ENV_WAIT}={raw!r}")
        return DEFAULT_WAIT_S


def _pid_alive(pid: int) -> bool:
    """True if ``pid`` still exists. Signal 0 checks without delivering."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Someone else's process — alive, just not ours to signal.
        return True
    except OSError:
        return True
    return True


def _read_slot(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _reap_if_stale(path: Path) -> bool:
    """Delete ``path`` if its owner is gone. True when a slot was freed.

    A half-written slot file (killed between create and write) parses as None
    and is also reaped — otherwise one unlucky kill would leak a slot forever.
    """
    data = _read_slot(path)
    if data is None:
        # Give a slot being written right now a moment before judging it.
        if time.time() - _mtime(path) < 5.0:
            return False
        path.unlink(missing_ok=True)
        return True
    pid = int(data.get("pid", 0) or 0)
    if _pid_alive(pid):
        return False
    logger.info(f"Reclaiming slot from dead pid {pid} ({path.name})")
    path.unlink(missing_ok=True)
    return True


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def active_runs() -> list[dict[str, Any]]:
    """Live slot payloads, stale ones reaped. Used by ``researchkit runs``."""
    directory = runs_dir()
    if not directory.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(directory.glob("slot-*.json")):
        data = _read_slot(path)
        if data is None or not _pid_alive(int(data.get("pid", 0) or 0)):
            _reap_if_stale(path)
            continue
        out.append(data)
    return out


def _claim(directory: Path, limit: int, payload: dict[str, Any]) -> Path | None:
    """Atomically take the first free slot, or None if all are held.

    ``O_CREAT | O_EXCL`` is the whole concurrency argument: the kernel decides
    the winner, so two processes racing for the last slot cannot both get it.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(limit):
        path = directory / f"slot-{index}.json"
        for _attempt in (1, 2):  # retry once, after reaping a stale holder
            try:
                fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                if not _reap_if_stale(path):
                    break  # genuinely held; try the next slot
                continue
            except OSError as e:
                logger.warning(f"Could not claim {path}: {e}")
                break
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            return path
    return None


@contextmanager
def run_slot(
    *,
    topic: str,
    run_id: str = "",
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> Iterator[None]:
    """Hold one of the concurrent-run slots for the duration of the block.

    Waits for a slot when the pool is full, and raises
    :class:`TooManyConcurrentRuns` if none frees up in time. The slot is always
    released — including on Ctrl-C, a provider blowing up, or a SIGTERM that
    Python turns into an exception — because a leaked slot shrinks the pool for
    every later run.
    """
    limit = max_concurrent_runs()
    if limit <= 0:  # explicitly disabled
        yield
        return

    directory = runs_dir()
    payload = {
        "pid": os.getpid(),
        "run_id": run_id,
        "topic": topic[:200],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    deadline = time.monotonic() + _wait_budget_s()
    warned = False
    path: Path | None = None
    while True:
        path = _claim(directory, limit, payload)
        if path is not None:
            break
        if not warned:
            warned = True
            held = active_runs()
            message = (
                f"All {limit} concurrent-run slots are busy; waiting for one to "
                f"free up. Holding: "
                + ", ".join(
                    f"pid {r.get('pid')} {str(r.get('topic', ''))[:40]!r}"
                    for r in held[:5]
                )
            )
            logger.warning(message, extra={"stage": "run_slots_full"})
            if progress:
                progress(
                    {
                        "stage": "run_slots_full",
                        "message": message,
                        "limit": limit,
                        "active": len(held),
                    }
                )
        if time.monotonic() >= deadline:
            raise TooManyConcurrentRuns(
                f"{limit} researchkit runs are already collecting from providers "
                f"and none finished within the wait budget. Raise {_ENV_MAX}, "
                f"lengthen {_ENV_WAIT}, or check for runaway runs: "
                f"researchkit runs"
            )
        time.sleep(_POLL_S)

    try:
        in_use = len(active_runs())
        # Alert before the ceiling bites, so a climbing run count is visible
        # while there is still room to react.
        if limit >= 3 and in_use >= max(2, int(limit * 0.8)):
            message = f"{in_use}/{limit} concurrent researchkit runs active"
            logger.warning(message, extra={"stage": "run_slots_high"})
            if progress:
                progress(
                    {
                        "stage": "run_slots_high",
                        "message": message,
                        "limit": limit,
                        "active": in_use,
                    }
                )
        yield
    finally:
        path.unlink(missing_ok=True)
