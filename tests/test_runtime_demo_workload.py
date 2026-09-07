"""Pytest coverage for atlas.runtime.demo_workload (Phase 0.75 spec
section 7)."""

from __future__ import annotations

import pytest

from atlas.models import TaskStatus
from atlas.runtime.demo_workload import (
    ACCESS_LIMITED_INDEX,
    DEMO_TASK_COUNT,
    EXTRACTION_UNRESOLVED_INDEX,
    LOGIN_REQUIRED_INDEX,
    TRANSIENT_RETRY_INDICES,
    build_demo_failure_injector,
    build_demo_tasks,
    demo_task_id,
)
from atlas.workers.base import WorkerError

pytestmark = pytest.mark.unit


def test_builds_exactly_fifty_unique_tasks():
    tasks = build_demo_tasks()
    assert len(tasks) == DEMO_TASK_COUNT
    assert len({t.task_id for t in tasks}) == DEMO_TASK_COUNT


def test_required_outcome_distribution():
    injector = build_demo_failure_injector()

    access_limited_id = demo_task_id(ACCESS_LIMITED_INDEX)
    with pytest.raises(WorkerError) as exc:
        injector.resolve(access_limited_id, 1)
    assert exc.value.category.value == "ANTI_BOT"

    extraction_id = demo_task_id(EXTRACTION_UNRESOLVED_INDEX)
    with pytest.raises(WorkerError) as exc:
        injector.resolve(extraction_id, 1)
    assert exc.value.category.value == "SELECTOR_UNCERTAINTY"

    login_id = demo_task_id(LOGIN_REQUIRED_INDEX)
    with pytest.raises(WorkerError) as exc:
        injector.resolve(login_id, 1)
    assert exc.value.category.value == "LOGIN_WALL"

    for idx in TRANSIENT_RETRY_INDICES:
        task_id = demo_task_id(idx)
        with pytest.raises(WorkerError):
            injector.resolve(task_id, 1)
        outcome = injector.resolve(task_id, 2)
        assert outcome.status == TaskStatus.SUCCESS

    # All other tasks succeed on the first attempt (no permanent failure
    # unless a test deliberately injects one).
    scripted_indices = {ACCESS_LIMITED_INDEX, EXTRACTION_UNRESOLVED_INDEX, LOGIN_REQUIRED_INDEX, *TRANSIENT_RETRY_INDICES}
    for i in range(DEMO_TASK_COUNT):
        if i in scripted_indices:
            continue
        outcome = injector.resolve(demo_task_id(i), 1)
        assert outcome.status == TaskStatus.SUCCESS
