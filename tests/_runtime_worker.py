"""Helper subprocess for tests/test_runtime_restart.py (Phase 0.75 spec
section 10). NOT a pytest test module itself - invoked as a standalone
script so it can simulate a truly crashed Atlas runtime process
(os._exit, run lock left held by a now-dead PID, no cleanup) without
killing the pytest process itself.

Usage:
    python _runtime_worker.py <tmp_dir> <run_id> <stop_after_completed_or_'none'>

If stop_after_completed is an integer, this process acquires the run
lock itself, drives the runtime to that many completed tasks, then hard
-exits via os._exit() WITHOUT releasing the lock - deliberately
simulating a crashed Atlas process (mirrors tests/_crash_recovery_worker.py's
proven pattern, now exercised through the Phase 0.75 AtlasRuntime engine
instead of the bare LangGraph governor).

If stop_after_completed is 'none', the process calls the normal, public
`AtlasRuntime.resume()` API (acquire -> execute -> release) and exits
normally - used for the final "resume to completion" step.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.config import load_settings  # noqa: E402
from atlas.orchestration.run_lock import RUN_ALREADY_ACTIVE  # noqa: E402
from atlas.runtime.demo_workload import DemoWorker, build_demo_failure_injector, build_demo_tasks  # noqa: E402
from atlas.runtime.engine import AtlasRuntime  # noqa: E402


def _build_settings(tmp_dir: Path):
    settings = load_settings(
        state_db=tmp_dir / "state" / "atlas_state.sqlite",
        checkpoint_db=tmp_dir / "state" / "atlas_checkpoints.sqlite",
        output_dir=tmp_dir / "output",
        logs_dir=tmp_dir / "logs",
        browser_profile=tmp_dir / "profile",
        agents_dir=tmp_dir / "agents",
        skills_dir=tmp_dir / "skills",
        batch_size=5,
        retry_budget=2,
    )
    settings.ensure_directories()
    return settings


def main() -> None:
    tmp_dir = Path(sys.argv[1])
    run_id = sys.argv[2]
    stop_after_raw = sys.argv[3]
    stop_after = None if stop_after_raw == "none" else int(stop_after_raw)

    settings = _build_settings(tmp_dir)
    tasks = build_demo_tasks()
    worker = DemoWorker(build_demo_failure_injector())

    if stop_after is None:
        # Normal resume path: public API, acquires + releases the lock.
        runtime = AtlasRuntime(settings, run_id, tasks, worker, batch_size=5)
        result = runtime.resume()
        print(f"RUNTIME_WORKER_PROGRESS completed={result.progress.completed} status={result.status}")
        sys.stdout.flush()
        return

    # Crash-simulation path: acquire the lock ourselves, drive the
    # engine's internal (locked) execution directly, then hard-exit
    # WITHOUT releasing the lock - a real crash leaves the lock file
    # behind, owned by a PID that is now dead.
    runtime = AtlasRuntime(settings, run_id, tasks, worker, batch_size=5, stop_after_completed=stop_after)
    lock_status = runtime.run_lock.try_acquire(run_id=run_id)
    if lock_status.status == RUN_ALREADY_ACTIVE:
        print("RUN_ALREADY_ACTIVE")
        sys.stdout.flush()
        os._exit(3)

    result = runtime._execute_locked(resuming=False)  # noqa: SLF001 - deliberate test-only internal use
    print(f"RUNTIME_WORKER_PROGRESS completed={result.progress.completed} status={result.status}")
    sys.stdout.flush()  # os._exit() bypasses normal stdio flushing

    # Deliberately simulate an unexpected crash: no run_lock.release().
    os._exit(1)


if __name__ == "__main__":
    main()
