"""Pytest coverage for atlas.orchestration.run_lock (Phase 0.5 spec
section 5: process-safe single-run guard). Uses tmp_path only."""

from __future__ import annotations

import json

import pytest

from atlas.orchestration.run_lock import RUN_ALREADY_ACTIVE, RUN_LOCK_ACQUIRED, RunLock
from atlas.utils.procutil import current_pid

pytestmark = pytest.mark.unit


def test_try_acquire_succeeds_when_no_run_active(tmp_path):
    run_lock = RunLock(tmp_path)
    status = run_lock.try_acquire(run_id="run-1")
    assert status.status == RUN_LOCK_ACQUIRED
    assert status.run_id == "run-1"
    assert status.pid == current_pid()
    run_lock.release()


def test_second_invocation_gets_run_already_active(tmp_path):
    run_lock_a = RunLock(tmp_path)
    run_lock_a.try_acquire(run_id="run-morning")

    run_lock_b = RunLock(tmp_path)
    status_b = run_lock_b.try_acquire(run_id="run-second-invocation")
    assert status_b.status == RUN_ALREADY_ACTIVE
    assert status_b.run_id == "run-morning"

    run_lock_a.release()


def test_current_holder_is_read_only_and_does_not_acquire(tmp_path):
    run_lock_a = RunLock(tmp_path)
    run_lock_a.try_acquire(run_id="run-1")

    run_lock_b = RunLock(tmp_path)
    holder = run_lock_b.current_holder()
    assert holder is not None
    assert holder.run_id == "run-1"
    # run_lock_b must not have acquired anything.
    assert not run_lock_b._lock.is_held_by_self

    run_lock_a.release()


def test_current_holder_none_when_no_run_active(tmp_path):
    run_lock = RunLock(tmp_path)
    assert run_lock.current_holder() is None


def test_stale_run_lock_from_crashed_process_is_recovered(tmp_path):
    lock_file = tmp_path / "atlas_run.lock"
    dead_pid = 999_997
    lock_file.write_text(
        json.dumps(
            {
                "pid": dead_pid,
                "hostname": "x",
                "acquired_at": "2000-01-01T00:00:00+00:00",
                "metadata": {"run_id": "crashed-run"},
            }
        ),
        encoding="utf-8",
    )

    run_lock = RunLock(tmp_path)
    status = run_lock.try_acquire(run_id="fresh-run-after-crash")
    assert status.status == RUN_LOCK_ACQUIRED
    run_lock.release()


def test_context_manager_raises_runtime_error_when_already_active(tmp_path):
    run_lock_a = RunLock(tmp_path)
    run_lock_a.try_acquire(run_id="run-1")

    with pytest.raises(RuntimeError, match=RUN_ALREADY_ACTIVE):
        with RunLock(tmp_path):
            pass

    run_lock_a.release()
