"""Tests for the cross-process concurrent-run ceiling."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from researchkit.run_limit import (
    TooManyConcurrentRuns,
    active_runs,
    max_concurrent_runs,
    run_slot,
    runs_dir,
)


@pytest.fixture(autouse=True)
def _isolated_slots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch the real ~/.researchkit/runs during tests."""
    monkeypatch.setenv("RESEARCHKIT_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("RESEARCHKIT_RUN_SLOT_WAIT_S", "0")


class TestConfiguration:
    def test_defaults_and_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("RESEARCHKIT_MAX_CONCURRENT_RUNS", raising=False)
        assert max_concurrent_runs() == 10
        monkeypatch.setenv("RESEARCHKIT_MAX_CONCURRENT_RUNS", "3")
        assert max_concurrent_runs() == 3
        # Garbage must not silently disable the ceiling.
        monkeypatch.setenv("RESEARCHKIT_MAX_CONCURRENT_RUNS", "banana")
        assert max_concurrent_runs() == 10

    def test_zero_disables_the_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RESEARCHKIT_MAX_CONCURRENT_RUNS", "0")
        with run_slot(topic="a"), run_slot(topic="b"), run_slot(topic="c"):
            assert active_runs() == []  # no bookkeeping at all when disabled


class TestCeiling:
    def test_holds_a_slot_and_releases_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RESEARCHKIT_MAX_CONCURRENT_RUNS", "2")
        with run_slot(topic="held", run_id="r1"):
            active = active_runs()
            assert [a["topic"] for a in active] == ["held"]
            assert active[0]["pid"] == os.getpid()
        assert active_runs() == []

    def test_refuses_past_the_ceiling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RESEARCHKIT_MAX_CONCURRENT_RUNS", "2")
        with (
            run_slot(topic="one"),
            run_slot(topic="two"),
            pytest.raises(TooManyConcurrentRuns) as excinfo,
            run_slot(topic="three"),
        ):
            pytest.fail("third run must not start")
        # The message has to lead somewhere.
        assert "researchkit runs" in str(excinfo.value)

    def test_slot_is_released_when_the_run_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RESEARCHKIT_MAX_CONCURRENT_RUNS", "1")
        with pytest.raises(ValueError), run_slot(topic="boom"):
            raise ValueError("provider exploded")
        # A leaked slot would shrink the pool for every later run.
        assert active_runs() == []
        with run_slot(topic="next"):
            assert len(active_runs()) == 1

    def test_alerts_before_it_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RESEARCHKIT_MAX_CONCURRENT_RUNS", "3")
        events: list[dict] = []
        with (
            run_slot(topic="a"),
            run_slot(topic="b"),
            run_slot(topic="c", progress=events.append),
        ):
            pass
        stages = [e["stage"] for e in events]
        assert "run_slots_high" in stages
        assert events[0]["active"] == 3 and events[0]["limit"] == 3


class TestStaleSlots:
    def test_a_slot_owned_by_a_dead_pid_is_reclaimed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RESEARCHKIT_MAX_CONCURRENT_RUNS", "1")
        # A process that died without cleaning up (kill -9, crash, reboot).
        directory = runs_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "slot-0.json").write_text(
            json.dumps({"pid": 999_999_998, "topic": "crashed run"}),
            encoding="utf-8",
        )

        # Without reaping, the pool would be permanently one slot smaller.
        with run_slot(topic="after the crash"):
            assert [a["topic"] for a in active_runs()] == ["after the crash"]

    def test_a_live_holder_is_never_reaped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RESEARCHKIT_MAX_CONCURRENT_RUNS", "1")
        directory = runs_dir()
        directory.mkdir(parents=True, exist_ok=True)
        # This test process is unambiguously alive.
        (directory / "slot-0.json").write_text(
            json.dumps({"pid": os.getpid(), "topic": "someone else"}),
            encoding="utf-8",
        )
        with pytest.raises(TooManyConcurrentRuns), run_slot(topic="mine"):
            pytest.fail("must not steal a live slot")


class TestCrossProcess:
    def test_two_processes_share_one_ceiling(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The whole point: the storm was ~200 separate processes, so an
        # in-process semaphore would have counted one run and let the rest go.
        monkeypatch.setenv("RESEARCHKIT_MAX_CONCURRENT_RUNS", "1")
        with run_slot(topic="held by the test process"):
            child = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from researchkit.run_limit import run_slot, TooManyConcurrentRuns\n"
                    "try:\n"
                    "    with run_slot(topic='child'): print('GOT SLOT')\n"
                    "except TooManyConcurrentRuns: print('REFUSED')\n",
                ],
                capture_output=True,
                text=True,
                env={**os.environ},
                timeout=120,
            )
        assert child.stdout.strip() == "REFUSED", child.stderr

    def test_racing_claimants_cannot_both_win(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # O_EXCL is the entire mutual-exclusion argument; prove it under load.
        monkeypatch.setenv("RESEARCHKIT_MAX_CONCURRENT_RUNS", "4")
        got: list[int] = []
        refused: list[int] = []
        barrier = threading.Barrier(12)

        def attempt(n: int) -> None:
            barrier.wait()
            try:
                with run_slot(topic=f"racer-{n}"):
                    got.append(n)
                    time.sleep(0.15)  # hold it while the others race
            except TooManyConcurrentRuns:
                refused.append(n)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert len(got) == 4, f"expected exactly the cap, got {len(got)}"
        assert len(refused) == 8
        assert active_runs() == []
