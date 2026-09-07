"""Pytest coverage for atlas.utils.pidlock (Phase 0.5 spec sections 4/5).

Uses tmp_path only - never the real authenticated Atlas browser profile.
"""

from __future__ import annotations

import json

import pytest

from atlas.utils.pidlock import LockHeldError, PidLock
from atlas.utils.procutil import current_pid

pytestmark = pytest.mark.unit


def test_acquire_and_release_roundtrip(tmp_path):
    lock = PidLock(tmp_path / "test.lock")
    info = lock.acquire(metadata={"purpose": "test"})
    assert info.pid == current_pid()
    assert lock.is_held_by_self
    assert (tmp_path / "test.lock").exists()

    lock.release()
    assert not lock.is_held_by_self
    assert not (tmp_path / "test.lock").exists()


def test_second_lock_blocked_while_first_process_is_live(tmp_path):
    lock_path = tmp_path / "test.lock"
    lock_a = PidLock(lock_path)
    lock_a.acquire()

    lock_b = PidLock(lock_path)
    with pytest.raises(LockHeldError) as excinfo:
        lock_b.acquire()
    assert excinfo.value.info.pid == current_pid()

    lock_a.release()


def test_stale_lock_from_dead_pid_is_reclaimed(tmp_path):
    lock_path = tmp_path / "test.lock"
    # A PID that is virtually guaranteed not to be running.
    dead_pid = 999_999
    lock_path.write_text(
        json.dumps({"pid": dead_pid, "hostname": "x", "acquired_at": "2000-01-01T00:00:00+00:00", "metadata": {}}),
        encoding="utf-8",
    )

    lock = PidLock(lock_path)
    info = lock.acquire()
    assert info.pid == current_pid()
    lock.release()


def test_corrupt_lock_file_falls_back_to_age_based_staleness(tmp_path):
    lock_path = tmp_path / "test.lock"
    lock_path.write_text("not valid json", encoding="utf-8")

    lock = PidLock(lock_path, corrupt_stale_after_seconds=0)
    info = lock.acquire()
    assert info.pid == current_pid()
    lock.release()


def test_release_never_removes_a_lock_owned_by_another_pid(tmp_path):
    lock_path = tmp_path / "test.lock"
    other_pid = 999_998
    lock_path.write_text(
        json.dumps(
            {"pid": other_pid, "hostname": "x", "acquired_at": "2999-01-01T00:00:00+00:00", "metadata": {}}
        ),
        encoding="utf-8",
    )

    lock = PidLock(lock_path)
    lock._held = True  # simulate a caller believing it holds the lock
    lock.release()
    # The file must be untouched since it is not actually owned by us,
    # UNLESS the other_pid happens to be stale (it is a made-up, almost
    # certainly dead PID, but release() only checks pid-match, not
    # liveness, so the file must survive regardless).
    assert lock_path.exists()
    on_disk = json.loads(lock_path.read_text(encoding="utf-8"))
    assert on_disk["pid"] == other_pid


def test_context_manager_acquires_and_releases(tmp_path):
    lock_path = tmp_path / "test.lock"
    with PidLock(lock_path) as lock:
        assert lock.is_held_by_self
        assert lock_path.exists()
    assert not lock_path.exists()
