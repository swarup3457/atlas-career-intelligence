"""Atlas bounded concurrency abstraction (Phase 0.75 spec section 6).

A small, generic, reusable primitive that bounds how many tasks may run
concurrently and guarantees the SAME task id is never in flight twice at
once. This prepares Atlas for future parallel workers without assuming
they can share one Playwright Page/browser context - by design this
module knows nothing about browsers at all; it is pure task-execution
plumbing.

Today's Phase 0.75 demo workload uses deterministic, non-web fake workers
only - this executor is deliberately never wired to open multiple real
browser windows. See docs/RUNTIME.md "Future browser concurrency
constraints" for the documented plan for when real parallel browser
workers are introduced.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Generic, TypeVar

T = TypeVar("T")


class DuplicateInFlightTaskError(RuntimeError):
    """Raised when a caller attempts to submit a task_id that is already
    running concurrently - the scheduler must never process the same
    task concurrently twice."""


class BoundedExecutor(Generic[T]):
    """Runs callables bounded by `max_parallel_tasks`, tracking in-flight
    task ids so the same task_id can never be submitted twice while it is
    still running.

    Usage:
        executor = BoundedExecutor(max_parallel_tasks=4)
        try:
            results = executor.run_batch({"task-1": fn1, "task-2": fn2})
        finally:
            executor.shutdown()
    """

    def __init__(self, max_parallel_tasks: int = 1):
        if max_parallel_tasks < 1:
            raise ValueError("max_parallel_tasks must be >= 1")
        self.max_parallel_tasks = max_parallel_tasks
        self._pool = ThreadPoolExecutor(max_workers=max_parallel_tasks)
        self._in_flight: set[str] = set()
        self._lock = threading.Lock()
        self._running_count = 0
        self._max_observed_concurrency = 0

    @property
    def max_observed_concurrency(self) -> int:
        """Highest number of tasks this executor has ever ACTUALLY run at
        once (not merely submitted/queued) - exposed primarily so tests
        can assert the bound was respected."""
        return self._max_observed_concurrency

    def submit(self, task_id: str, fn: Callable[[], T]) -> "Future[T]":
        with self._lock:
            if task_id in self._in_flight:
                raise DuplicateInFlightTaskError(
                    f"Task {task_id!r} is already in flight - cannot submit concurrently twice."
                )
            self._in_flight.add(task_id)

        def _wrapped() -> T:
            with self._lock:
                self._running_count += 1
                self._max_observed_concurrency = max(self._max_observed_concurrency, self._running_count)
            try:
                return fn()
            finally:
                with self._lock:
                    self._running_count -= 1
                    self._in_flight.discard(task_id)

        return self._pool.submit(_wrapped)

    def run_batch(self, work: dict[str, Callable[[], T]]) -> dict[str, "Future[T]"]:
        """Submit an entire batch (dict of task_id -> zero-arg callable)
        and return the dict of Futures (still bounded by
        max_parallel_tasks worker threads - this call does not block)."""
        return {task_id: self.submit(task_id, fn) for task_id, fn in work.items()}

    def shutdown(self, wait: bool = True) -> None:
        self._pool.shutdown(wait=wait)

    def __enter__(self) -> "BoundedExecutor[T]":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.shutdown()
