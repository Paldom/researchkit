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


@pytest.fixture(autouse=True)
def _clean_quota_registry() -> object:
    # The exhausted-pool registry is process-wide by design; tests must not
    # leak a "quota exhausted" verdict into each other.
    from researchkit.failures import clear_exhausted

    clear_exhausted()
    yield
    clear_exhausted()


@pytest.fixture(autouse=True)
def _no_network_archiving(monkeypatch: pytest.MonkeyPatch) -> None:
    # run_project archives cited sources before its writers (PLAN 2.4); unit
    # tests must never hit the network. Tests that exercise fetching patch
    # researchkit.materials.safe_fetch themselves, which overrides this.
    import researchkit.materials as materials
    from researchkit.safe_io import FetchResult

    monkeypatch.setattr(
        materials,
        "safe_fetch",
        lambda url, **kw: FetchResult(url, url, error="network disabled in tests"),
    )
