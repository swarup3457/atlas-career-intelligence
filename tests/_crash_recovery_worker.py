"""Helper subprocess for tests/test_crash_recovery.py (Phase 0.5 spec
section 7). NOT a pytest test module itself - invoked as a standalone
script so it can simulate an unexpected process crash (os._exit, no
cleanup, no lock release) without killing the pytest process itself.

Usage:
    python _crash_recovery_worker.py <state_dir> <checkpoint_db> <thread_id> <run_id> <stop_after_n>

Processes items with a real LangGraph governor graph (deterministic,
no LLM, no internet) and hard-exits via os._exit() after `stop_after_n`
items have been completed, WITHOUT releasing the run lock and WITHOUT
graceful shutdown - deliberately simulating a crashed Atlas process.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.models import TaskStatus  # noqa: E402
from atlas.orchestration.checkpoints import open_checkpointer, thread_config  # noqa: E402
from atlas.orchestration.graph import build_graph  # noqa: E402
from atlas.orchestration.run_lock import RunLock  # noqa: E402
from atlas.orchestration.state import RUN_STATUS_COMPLETE, initial_queue_state  # noqa: E402
from atlas.workers.base import BaseWorker, WorkerOutcome  # noqa: E402

ALL_ITEMS = ["A", "B", "C", "D", "E"]


class DeterministicWorker(BaseWorker):
    name = "crash-recovery-test-worker"

    def attempt(self, item: str, attempt_number: int) -> WorkerOutcome:
        return WorkerOutcome(status=TaskStatus.SUCCESS, payload={"item": item})


def main() -> None:
    state_dir = Path(sys.argv[1])
    checkpoint_db = Path(sys.argv[2])
    thread_id = sys.argv[3]
    run_id = sys.argv[4]
    stop_after_n = int(sys.argv[5])

    run_lock = RunLock(state_dir)
    status = run_lock.try_acquire(run_id=run_id)
    if status.status != "RUN_LOCK_ACQUIRED":
        print("RUN_ALREADY_ACTIVE")
        sys.exit(3)

    worker = DeterministicWorker()
    builder = build_graph(worker, retry_budget=1)
    config = thread_config(thread_id)

    with open_checkpointer(checkpoint_db) as checkpointer:
        graph = builder.compile(checkpointer=checkpointer)
        existing = graph.get_state(config).values
        seed = existing if existing else initial_queue_state(ALL_ITEMS)

        state = graph.invoke(seed, config)
        completed_count = len(state.get("completed_items", []))
        while completed_count < stop_after_n and state.get("run_status") != RUN_STATUS_COMPLETE:
            state = graph.invoke({}, config)
            completed_count = len(state.get("completed_items", []))

        print(f"CRASH_RECOVERY_WORKER_PROGRESS completed={completed_count}")
        sys.stdout.flush()  # os._exit() bypasses normal stdio flushing

    # Deliberately simulate an unexpected crash: no run_lock.release(),
    # no graceful shutdown, no further cleanup.
    os._exit(1)


if __name__ == "__main__":
    main()
