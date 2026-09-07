"""Pytest coverage for atlas.runtime.scheduler (Phase 0.75 spec section 4)."""

from __future__ import annotations

import pytest

from atlas.runtime.scheduler import CyclicDependencyError, TaskRecord, TaskScheduler

pytestmark = pytest.mark.unit


def test_orders_by_priority_desc_then_task_id():
    tasks = [
        TaskRecord(task_id="b", priority=0),
        TaskRecord(task_id="a", priority=5),
        TaskRecord(task_id="c", priority=5),
    ]
    ordered = TaskScheduler().order(tasks)
    assert ordered == ["a", "c", "b"]


def test_dependency_ordering_respected():
    tasks = [
        TaskRecord(task_id="child", dependency="parent"),
        TaskRecord(task_id="parent"),
    ]
    ordered = TaskScheduler().order(tasks)
    assert ordered.index("parent") < ordered.index("child")


def test_chained_dependencies_respected():
    tasks = [
        TaskRecord(task_id="c", dependency="b"),
        TaskRecord(task_id="a"),
        TaskRecord(task_id="b", dependency="a"),
    ]
    ordered = TaskScheduler().order(tasks)
    assert ordered.index("a") < ordered.index("b") < ordered.index("c")


def test_cyclic_dependency_raises():
    tasks = [
        TaskRecord(task_id="x", dependency="y"),
        TaskRecord(task_id="y", dependency="x"),
    ]
    with pytest.raises(CyclicDependencyError):
        TaskScheduler().order(tasks)


def test_unknown_dependency_raises():
    tasks = [TaskRecord(task_id="a", dependency="does-not-exist")]
    with pytest.raises(ValueError):
        TaskScheduler().order(tasks)


def test_duplicate_task_id_raises():
    tasks = [TaskRecord(task_id="a"), TaskRecord(task_id="a")]
    with pytest.raises(ValueError):
        TaskScheduler().order(tasks)


def test_group_batches_preserves_order():
    ids = ["a", "b", "c", "d", "e"]
    batches = TaskScheduler().group_batches(ids, batch_size=2)
    assert batches == [["a", "b"], ["c", "d"], ["e"]]


def test_group_batches_rejects_invalid_batch_size():
    with pytest.raises(ValueError):
        TaskScheduler().group_batches(["a"], batch_size=0)


def test_deterministic_across_multiple_calls():
    tasks = [TaskRecord(task_id=f"t{i}", priority=i % 3) for i in range(20)]
    first = TaskScheduler().order(tasks)
    second = TaskScheduler().order(tasks)
    assert first == second
