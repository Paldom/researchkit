"""Logging filters for context injection and run isolation."""

from __future__ import annotations

import logging

from researchkit.observability.context import get_run_id


class ContextInjectFilter(logging.Filter):
    """
    Injects consistent fields into every log record.

    Ensures formatters never get KeyError for expected fields like
    run_id, stage, and provider.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Always set run_id from contextvar if not already set
        if not hasattr(record, "run_id") or record.run_id is None:
            record.run_id = get_run_id()

        # Ensure optional fields have defaults
        if not hasattr(record, "stage") or record.stage is None:
            record.stage = "-"
        if not hasattr(record, "provider") or record.provider is None:
            record.provider = "-"

        # Always allow the record through (permissive filter)
        return True


class OnlyRunIdFilter(logging.Filter):
    """
    Filter that only allows records matching a specific run_id.

    Used for per-run log file handlers to isolate logs by run.
    """

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        return getattr(record, "run_id", "-") == self.run_id
