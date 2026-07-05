"""Per-run log file handler for isolated run logs."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path

from researchkit.observability.filters import ContextInjectFilter, OnlyRunIdFilter

# Format for per-run log files (slightly more detailed)
RUN_LOG_FORMAT = (
    "%(asctime)s %(levelname)-8s "
    "[%(name)s] [stage=%(stage)s] [provider=%(provider)s] "
    "%(message)s"
)


@contextmanager
def attach_run_file_handler(
    *,
    run_id: str,
    log_dir: Path,
    level: str = "DEBUG",
    filename: str | None = None,
):
    """
    Context manager that attaches a per-run log file handler.

    The handler only writes log records matching this run_id,
    enabling concurrent runs to have isolated log files.

    Args:
        run_id: The run ID to filter for
        log_dir: Directory for log files
        level: Log level for this handler
        filename: Custom filename (default: run_<run_id>.log)

    Yields:
        Path to the per-run log file
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / (filename or f"run_{run_id}.log")

    root = logging.getLogger()
    lvl = getattr(logging, level.upper(), logging.DEBUG)

    formatter = logging.Formatter(RUN_LOG_FORMAT)

    handler = logging.FileHandler(str(path), encoding="utf-8")
    handler.setLevel(lvl)
    handler.setFormatter(formatter)

    # Add filters: inject context fields, then filter by run_id
    handler.addFilter(ContextInjectFilter())
    handler.addFilter(OnlyRunIdFilter(run_id))

    root.addHandler(handler)

    try:
        root.debug(
            "Per-run log handler attached",
            extra={"stage": "logging_run_attach", "run_id": run_id},
        )
        yield path
    finally:
        root.debug(
            "Per-run log handler detaching",
            extra={"stage": "logging_run_detach", "run_id": run_id},
        )
        root.removeHandler(handler)
        handler.close()
