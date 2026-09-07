"""Pytest coverage for the controller abstraction / controller-neutral
boundary (Phase 0.5 spec section 22): deterministic Atlas operation must
not require Copilot, Codex, or any OpenAI API."""

from __future__ import annotations

import pytest

from atlas.controllers.base import ControllerRequest, NullController, get_controller

pytestmark = pytest.mark.unit


def test_get_controller_none_returns_null_controller():
    controller = get_controller("none")
    assert isinstance(controller, NullController)
    assert controller.name == "none"


def test_null_controller_never_raises_and_never_calls_out():
    controller = NullController()
    request = ControllerRequest(prompt="does not matter", context={})
    for op in (controller.reason, controller.classify, controller.extract, controller.review):
        response = op(request)
        assert response.metadata["noop"] is True
        assert response.metadata["controller"] == "none"


def test_unknown_controller_name_raises_value_error():
    with pytest.raises(ValueError):
        get_controller("not-a-real-controller")


def test_deterministic_graph_runs_with_null_controller_only(tmp_path):
    """Proves the LangGraph orchestration path completes end-to-end using
    only NullController-equivalent (i.e. no controller at all) - workers
    never depend on an LLM to produce a deterministic result."""
    from atlas.models import TaskStatus
    from atlas.orchestration.checkpoints import open_checkpointer, thread_config
    from atlas.orchestration.graph import build_graph, run_to_completion
    from atlas.orchestration.state import initial_queue_state
    from atlas.workers.base import BaseWorker, WorkerOutcome

    class DeterministicWorker(BaseWorker):
        name = "controller-boundary-test-worker"

        def attempt(self, item: str, attempt_number: int) -> WorkerOutcome:
            return WorkerOutcome(status=TaskStatus.SUCCESS, payload={"item": item})

    db_path = tmp_path / "controller_boundary_checkpoint.sqlite"
    builder = build_graph(DeterministicWorker(), retry_budget=1)
    seed = initial_queue_state(["A", "B"])
    config = thread_config("controller-boundary-test")

    with open_checkpointer(db_path) as checkpointer:
        graph = builder.compile(checkpointer=checkpointer)
        final_state = run_to_completion(graph, config, seed)

    assert final_state["run_status"] == "COMPLETE"
    assert set(final_state["completed_items"]) == {"A", "B"}
