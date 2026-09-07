"""Pytest coverage for atlas.orchestration.checkpoints (LangGraph
checkpointing, Phase 0.5 spec section 2). Uses tmp_path only."""

from __future__ import annotations

import pytest

from atlas.orchestration.checkpoints import open_checkpointer, thread_config

pytestmark = pytest.mark.unit


def test_open_checkpointer_creates_db_file(tmp_path):
    db_path = tmp_path / "checkpoints.sqlite"
    with open_checkpointer(db_path):
        pass
    assert db_path.exists()


def test_thread_config_shape():
    config = thread_config("my-thread")
    assert config == {"configurable": {"thread_id": "my-thread"}}


def test_checkpointer_persists_graph_state_across_reopen(tmp_path):
    """Mirrors the proven pattern in test_foundation_suite.py section I,
    isolated as a dedicated pytest case for the checkpointing contract."""
    from atlas.models import ErrorCategory, TaskStatus
    from atlas.orchestration.graph import build_graph, run_to_completion
    from atlas.orchestration.state import initial_queue_state
    from atlas.workers.base import BaseWorker, WorkerError, WorkerOutcome

    class FlakyOnceWorker(BaseWorker):
        name = "pytest-flaky-once-worker"

        def attempt(self, item: str, attempt_number: int) -> WorkerOutcome:
            if item == "B" and attempt_number == 1:
                raise WorkerError(ErrorCategory.TRANSIENT_NAVIGATION, "simulated first-attempt failure")
            return WorkerOutcome(status=TaskStatus.SUCCESS, payload={"item": item})

    db_path = tmp_path / "checkpoint_test.sqlite"
    worker = FlakyOnceWorker()
    builder = build_graph(worker, retry_budget=2)
    seed = initial_queue_state(["A", "B", "C"])
    config = thread_config("pytest-checkpoint-test")

    with open_checkpointer(db_path) as checkpointer:
        graph = builder.compile(checkpointer=checkpointer)
        final_state = run_to_completion(graph, config, seed)

    assert final_state["run_status"] == "COMPLETE"
    assert set(final_state["completed_items"]) == {"A", "B", "C"}

    with open_checkpointer(db_path) as checkpointer2:
        graph2 = builder.compile(checkpointer=checkpointer2)
        resumed_state = graph2.get_state(config).values
        assert resumed_state["run_status"] == "COMPLETE"
