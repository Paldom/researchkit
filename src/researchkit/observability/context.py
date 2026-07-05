"""Context variables for run correlation across threads and async tasks."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

# Context variable for tracking run_id across async tasks and threads
# asyncio.to_thread propagates contextvars, so this works in provider threads
run_id_var: ContextVar[str] = ContextVar("social_research_run_id", default="-")


def new_run_id() -> str:
    """Generate a new short, URL/file-safe run ID."""
    return uuid4().hex[:12]


def get_run_id() -> str:
    """Get the current run ID from context."""
    return run_id_var.get()


@contextmanager
def run_context(run_id: str | None = None):
    """
    Context manager that sets run_id for the duration of a run.

    Args:
        run_id: Optional run ID to use. If None, generates a new one.

    Yields:
        The run_id being used for this context.
    """
    rid = run_id or new_run_id()
    token = run_id_var.set(rid)
    try:
        yield rid
    finally:
        run_id_var.reset(token)
