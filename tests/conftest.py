"""Shared pytest fixtures.

Speeds up the retry-based tests by shrinking the unified backoff to near-zero
for the whole session — the tests assert retry *counts*/behavior, not wall-clock
timing, and the review flagged the suite spending ~2 minutes in real sleeps.
"""

from __future__ import annotations

import pytest

import researchkit.network_retry as network_retry


@pytest.fixture(autouse=True, scope="session")
def _fast_network_backoff() -> None:
    # Tiny backoff so retry tests don't sleep for real seconds.
    network_retry.DEFAULT_BACKOFF_MIN = 0.0
    network_retry.DEFAULT_BACKOFF_MAX = 0.01
    network_retry.DEFAULT_JITTER_MAX = 0.01
