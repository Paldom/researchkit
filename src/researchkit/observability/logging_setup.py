"""Application-level logging setup for CLI and Gradio frontends."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from researchkit.observability.filters import ContextInjectFilter

# Standard log format with context fields
LOG_FORMAT = (
    "%(asctime)s %(levelname)-8s "
    "[run=%(run_id)s] [%(name)s] [stage=%(stage)s] [provider=%(provider)s] "
    "%(message)s"
)

# Shorter format for console output
CONSOLE_FORMAT = "%(asctime)s %(levelname)-8s [%(stage)s] %(message)s"


def init_app_logging(
    *,
    log_dir: Path = Path(".logs"),
    level: str = "INFO",
    console: bool = True,
    console_level: str | None = None,
    global_log_filename: str = "social-research.log",
    max_bytes: int = 5_000_000,
    backup_count: int = 3,
) -> Path:
    """
    Configure root logging once per process.

    Safe to call multiple times; subsequent calls are no-ops.

    Args:
        log_dir: Directory for log files (created if needed)
        level: Log level for file handler (DEBUG, INFO, WARNING, ERROR)
        console: Whether to add console handler
        console_level: Log level for console (defaults to level)
        global_log_filename: Name of the global log file
        max_bytes: Max size before rotation
        backup_count: Number of backup files to keep

    Returns:
        Path to the global log file
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    global_log_path = log_dir / global_log_filename

    root = logging.getLogger()
    lvl = getattr(logging, level.upper(), logging.INFO)
    root.setLevel(lvl)

    # Idempotency: don't stack handlers on repeated calls
    if getattr(root, "_social_research_logging_configured", False):
        return global_log_path
    root._social_research_logging_configured = True  # type: ignore[attr-defined]

    # Context injection filter for root logger
    inject_filter = ContextInjectFilter()
    root.addFilter(inject_filter)

    # Global rotating file handler (all runs)
    file_formatter = logging.Formatter(LOG_FORMAT)
    file_handler = RotatingFileHandler(
        filename=str(global_log_path),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(lvl)
    file_handler.setFormatter(file_formatter)
    file_handler.addFilter(inject_filter)
    root.addHandler(file_handler)

    # Console handler (optional)
    if console:
        console_formatter = logging.Formatter(CONSOLE_FORMAT)
        console_handler = logging.StreamHandler()
        console_lvl = getattr(logging, (console_level or level).upper(), lvl)
        console_handler.setLevel(console_lvl)
        console_handler.setFormatter(console_formatter)
        console_handler.addFilter(inject_filter)
        root.addHandler(console_handler)

    # Silence noisy third-party loggers
    for noisy in ("httpx", "urllib3", "httpcore", "openai", "google"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    root.debug("Logging initialized", extra={"stage": "logging_init"})
    return global_log_path
