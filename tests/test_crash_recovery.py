"""Deterministic crash-recovery integration test (Phase 0.5 spec section
7). No LLM, no internet. Simulates:

    1. start an Atlas run (subprocess A) and complete several fake tasks;
    2. the subprocess crashes (os._exit, no cleanup) partway through;
    3. a fresh process (subprocess B) launches;
    4. verify run-lock recovery (stale lock from the dead PID is reclaimed);
    5. verify checkpoint recovery (LangGraph state read back correctly);
    6. verify completed tasks are not repeated;
    7. verify remaining tasks continue;
    8. verify final completion.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from atlas.orchestration.checkpoints import open_checkpointer, thread_config
from atlas.orchestration.run_lock import RunLock
from atlas.utils.procutil import is_pid_running

pytestmark = pytest.mark.integration

WORKER_SCRIPT = Path(__file__).resolve().parent / "_crash_recovery_worker.py"
ALL_ITEMS = ["A", "B", "C", "D", "E"]


def _run_worker(state_dir: Path, checkpoint_db: Path, thread_id: str, run_id: str, stop_after_n: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(WORKER_SCRIPT), str(state_dir), str(checkpoint_db), thread_id, run_id, str(stop_after_n)],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_crash_and_resume_completes_without_repeating_tasks(tmp_path):
    state_dir = tmp_path / "state"
    checkpoint_db = tmp_path / "checkpoints.sqlite"
    thread_id = "crash-recovery-thread"

    # --- Step 1/2: start a run and "crash" partway through. ---
    crash_result = _run_worker(state_dir, checkpoint_db, thread_id, run_id="run-morning", stop_after_n=2)
    assert "CRASH_RECOVERY_WORKER_PROGRESS" in crash_result.stdout, crash_result.stdout + crash_result.stderr
    # os._exit(1) means the process did not exit cleanly (by design).
    assert crash_result.returncode == 1

    # --- Step: verify the run lock was left behind (crash == no release) ---
    lock_file = state_dir / "atlas_run.lock"
    assert lock_file.exists(), "crashed process should have left its run lock behind"
    run_lock_probe = RunLock(state_dir)
    holder = run_lock_probe.current_holder()
    assert holder is not None
    assert holder.run_id == "run-morning"
    assert not is_pid_running(holder.pid), "the worker subprocess should have actually exited"

    # --- Step 3/4: launch a fresh process; verify run-lock recovery. ---
    resume_result = _run_worker(state_dir, checkpoint_db, thread_id, run_id="run-resume", stop_after_n=5)
    assert "CRASH_RECOVERY_WORKER_PROGRESS" in resume_result.stdout, resume_result.stdout + resume_result.stderr
    assert "completed=5" in resume_result.stdout

    # --- Step 5/6/7/8: verify checkpoint recovery, no repeated tasks, full completion. ---
    with open_checkpointer(checkpoint_db) as checkpointer:
        from atlas.workers.base import BaseWorker, WorkerOutcome
        from atlas.models import TaskStatus
        from atlas.orchestration.graph import build_graph

        class _Dummy(BaseWorker):
            name = "verify-only"

            def attempt(self, item: str, attempt_number: int) -> WorkerOutcome:
                return WorkerOutcome(status=TaskStatus.SUCCESS, payload={})

        builder = build_graph(_Dummy(), retry_budget=1)
        graph = builder.compile(checkpointer=checkpointer)
        final_state = graph.get_state(thread_config(thread_id)).values

    assert final_state["run_status"] == "COMPLETE"
    assert sorted(final_state["completed_items"]) == sorted(ALL_ITEMS)
    # No task repeated: each item appears in completed_items exactly once.
    assert len(final_state["completed_items"]) == len(set(final_state["completed_items"]))
    # Each item was attempted exactly once (retry_budget=1, no simulated
    # failures in this deterministic worker, so no retries were needed).
    for item in ALL_ITEMS:
        assert final_state["retry_counts"][item] == 1

    # --- Final: a third invocation with the SAME run_id must not be
    # blocked (the resume run already released its lock cleanly since it
    # ran to completion inside `with open_checkpointer(...)` and returned
    # normally... except this worker script always os._exit()s, so the
    # lock is still held by the (now-dead) resume PID. A brand new run_id
    # must still be able to reclaim it, proving stale-lock recovery works
    # repeatedly, not just once.
    third_result = _run_worker(state_dir, checkpoint_db, thread_id, run_id="run-third", stop_after_n=5)
    assert "completed=5" in third_result.stdout
