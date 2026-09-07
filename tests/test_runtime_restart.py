"""Deterministic restart/resume end-to-end test for the Phase 0.75
runtime shell (spec section 10). No LLM, no internet.

Process A: drives AtlasRuntime to a partial point then crashes (os._exit,
run lock left held by a dead PID, no cleanup).
Process B: a fresh process calls AtlasRuntime.resume() via the public
CLI-equivalent API and must:
    - reclaim the stale run lock
    - resume the SAME run (not start a new one)
    - not repeat already-completed work
    - continue pending retries / remaining tasks
    - reach COMPLETE
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from atlas.orchestration.run_lock import RunLock
from atlas.utils.procutil import is_pid_running

pytestmark = pytest.mark.integration

WORKER_SCRIPT = Path(__file__).resolve().parent / "_runtime_worker.py"


def _run_worker(tmp_dir: Path, run_id: str, stop_after) -> subprocess.CompletedProcess:
    stop_arg = "none" if stop_after is None else str(stop_after)
    return subprocess.run(
        [sys.executable, str(WORKER_SCRIPT), str(tmp_dir), run_id, stop_arg],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_crash_and_resume_completes_full_demo_workload_without_repeating(tmp_path):
    run_id = "restart-e2e-run"

    # --- Process A: drive to a partial point, then "crash". ---
    crash_result = _run_worker(tmp_path, run_id, stop_after=15)
    assert "RUNTIME_WORKER_PROGRESS" in crash_result.stdout, crash_result.stdout + crash_result.stderr
    assert "completed=15" in crash_result.stdout
    assert crash_result.returncode == 1  # os._exit(1): did not exit cleanly, by design

    state_dir = tmp_path / "state"
    lock_file = state_dir / "atlas_run.lock"
    assert lock_file.exists(), "crashed process should have left its run lock behind"

    probe = RunLock(state_dir)
    holder = probe.current_holder()
    assert holder is not None
    assert holder.run_id == run_id
    assert not is_pid_running(holder.pid), "the worker subprocess should have actually exited"

    # --- Process B: fresh process resumes the SAME run. ---
    resume_result = _run_worker(tmp_path, run_id, stop_after=None)
    assert "RUNTIME_WORKER_PROGRESS" in resume_result.stdout, resume_result.stdout + resume_result.stderr
    assert "status=COMPLETE" in resume_result.stdout
    assert "completed=50" in resume_result.stdout

    # --- Verify durable state reflects the same run reaching COMPLETE. ---
    from atlas.persistence.sqlite import StateStore

    with StateStore(state_dir / "atlas_state.sqlite") as store:
        row = store.get_run(run_id)
        assert row is not None
        assert row["status"] == "COMPLETE"

    # A third resume call must be idempotent (no duplicated work, still COMPLETE).
    third_result = _run_worker(tmp_path, run_id, stop_after=None)
    assert "completed=50" in third_result.stdout
    assert "status=COMPLETE" in third_result.stdout
