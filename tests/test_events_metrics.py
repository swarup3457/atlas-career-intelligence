"""Pytest coverage for atlas.orchestration.events + metrics (Phase 0.5
spec sections 12/13)."""

from __future__ import annotations

import pytest

from atlas.orchestration.events import EventBus, EventType
from atlas.orchestration.metrics import RunMetrics, metrics_from_queue_state
from atlas.orchestration.state import initial_queue_state

pytestmark = pytest.mark.unit


def test_publish_records_event_and_notifies_subscribers():
    bus = EventBus(run_id="run-1")
    received = []
    bus.subscribe(received.append)

    event = bus.publish(EventType.RUN_STARTED, detail={"note": "test"})

    assert event.event_type == EventType.RUN_STARTED
    assert event.run_id == "run-1"
    assert received == [event]
    assert bus.events() == [event]


def test_publish_unknown_event_type_rejected():
    bus = EventBus(run_id="run-1")
    with pytest.raises(ValueError):
        bus.publish("NOT_A_REAL_EVENT_TYPE")


def test_publish_writes_json_lines_sink(tmp_path):
    sink_path = tmp_path / "events.jsonl"
    bus = EventBus(run_id="run-1", sink_path=sink_path)
    bus.publish(EventType.TASK_STARTED, task_id="task-1")
    bus.publish(EventType.TASK_COMPLETED, task_id="task-1")

    lines = sink_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2


def test_metrics_observe_event_updates_counters():
    metrics = RunMetrics()
    bus = EventBus(run_id="run-1")
    bus.subscribe(metrics.observe_event)

    bus.publish(EventType.TASK_STARTED, task_id="task-1")
    bus.publish(EventType.TASK_RETRY, task_id="task-1")
    bus.publish(EventType.TASK_COMPLETED, task_id="task-1")
    bus.publish(EventType.TASK_FAILED, task_id="task-2")
    bus.publish(EventType.ACCESS_LIMITED, task_id="task-3")

    assert metrics.attempted_tasks == 1
    assert metrics.retry_count == 1
    assert metrics.completed_tasks == 1
    assert metrics.failed_count == 1
    assert metrics.access_limited_count == 1
    assert metrics.elapsed_seconds >= 0


def test_metrics_from_queue_state():
    state = initial_queue_state(["A", "B", "C"])
    state["completed_items"] = ["A"]
    state["remaining_items"] = ["B", "C"]
    state["retry_counts"] = {"B": 2}
    state["failed_items"] = []
    state["access_limited_items"] = ["C"]
    state["attempt_log"] = [{"item": "A"}, {"item": "B"}, {"item": "B"}]

    metrics = metrics_from_queue_state(state)
    assert metrics.planned_tasks == 3
    assert metrics.completed_tasks == 1
    assert metrics.remaining_tasks == 2
    assert metrics.attempted_tasks == 3
    assert metrics.retry_count == 1
    assert metrics.access_limited_count == 1
