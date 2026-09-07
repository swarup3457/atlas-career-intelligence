"""Pytest coverage for atlas.runtime.concurrency.BoundedExecutor (Phase
0.75 spec section 6)."""

from __future__ import annotations

import threading
import time

import pytest

from atlas.runtime.concurrency import BoundedExecutor, DuplicateInFlightTaskError

pytestmark = pytest.mark.unit


def test_respects_max_parallel_tasks_bound():
    executor = BoundedExecutor(max_parallel_tasks=2)
    try:
        barrier_count = {"value": 0}
        lock = threading.Lock()

        def work():
            with lock:
                barrier_count["value"] += 1
            time.sleep(0.1)
            with lock:
                barrier_count["value"] -= 1
            return "ok"

        futures = executor.run_batch({f"task-{i}": work for i in range(6)})
        results = [f.result(timeout=5) for f in futures.values()]
        assert results == ["ok"] * 6
        assert executor.max_observed_concurrency <= 2
    finally:
        executor.shutdown()


def test_cannot_submit_same_task_id_concurrently_twice():
    executor = BoundedExecutor(max_parallel_tasks=2)
    try:
        started = threading.Event()
        release = threading.Event()

        def slow():
            started.set()
            release.wait(timeout=5)
            return "done"

        future = executor.submit("dup-task", slow)
        started.wait(timeout=5)
        with pytest.raises(DuplicateInFlightTaskError):
            executor.submit("dup-task", lambda: "second")
        release.set()
        assert future.result(timeout=5) == "done"
    finally:
        executor.shutdown()


def test_task_id_can_be_resubmitted_after_completion():
    executor = BoundedExecutor(max_parallel_tasks=1)
    try:
        first = executor.submit("task-1", lambda: "first").result(timeout=5)
        second = executor.submit("task-1", lambda: "second").result(timeout=5)
        assert (first, second) == ("first", "second")
    finally:
        executor.shutdown()


def test_rejects_invalid_max_parallel_tasks():
    with pytest.raises(ValueError):
        BoundedExecutor(max_parallel_tasks=0)


def test_context_manager_shuts_down():
    with BoundedExecutor(max_parallel_tasks=1) as executor:
        assert executor.submit("t", lambda: 1).result(timeout=5) == 1
