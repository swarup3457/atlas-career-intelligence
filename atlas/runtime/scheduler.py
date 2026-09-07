"""Atlas generic task scheduler (Phase 0.75 spec section 4).

A TaskRecord carries generic scheduling metadata - priority, batch label,
retry_at/next_attempt_at timestamps, and an optional dependency - on top
of the plain string item ids the existing, already-proven LangGraph
governor (atlas/orchestration/graph.py) understands. This module never
duplicates the governor's retry/checkpoint/terminal-state bookkeeping; it
only decides the deterministic INITIAL processing order fed into
`QueueState.remaining_items`, so the proven single-node governor keeps
its existing, tested queue-pop semantics unchanged.

No job-specific priorities are defined here - callers (e.g.
atlas/runtime/demo_workload.py) supply fake/demo metadata only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class TaskRecord:
    """Generic scheduling metadata for one task/item."""

    task_id: str
    priority: int = 0
    batch: Optional[str] = None
    dependency: Optional[str] = None  # task_id this task must follow, if any
    retry_at: Optional[str] = None
    next_attempt_at: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "priority": self.priority,
            "batch": self.batch,
            "dependency": self.dependency,
            "retry_at": self.retry_at,
            "next_attempt_at": self.next_attempt_at,
            "metadata": self.metadata,
        }


class CyclicDependencyError(ValueError):
    """Raised when TaskRecord dependencies form a cycle - the scheduler
    can never produce a valid initial order in that case."""


class TaskScheduler:
    """Computes a deterministic initial processing order for a set of
    TaskRecords, respecting `dependency` (a task never precedes the task
    it depends on) and breaking remaining ties by `priority` (higher
    first) then `task_id` (lexical) for full determinism."""

    def order(self, tasks: list[TaskRecord]) -> list[str]:
        by_id: dict[str, TaskRecord] = {t.task_id: t for t in tasks}
        if len(by_id) != len(tasks):
            raise ValueError("Duplicate task_id values supplied to TaskScheduler.order().")

        for t in tasks:
            if t.dependency is not None and t.dependency not in by_id:
                raise ValueError(f"Task {t.task_id!r} depends on unknown task {t.dependency!r}.")

        # Kahn's algorithm: at most one dependency per task, so the
        # "graph" is a forest - a cycle can only occur via a dependency
        # chain looping back on itself.
        remaining_deps: dict[str, Optional[str]] = {t.task_id: t.dependency for t in tasks}
        resolved: list[str] = []
        resolved_set: set[str] = set()

        # Deterministic candidate ordering: priority desc, then task_id asc.
        def sort_key(task_id: str) -> tuple[int, str]:
            record = by_id[task_id]
            return (-record.priority, task_id)

        pending = set(by_id.keys())
        while pending:
            ready = sorted(
                (
                    task_id
                    for task_id in pending
                    if remaining_deps[task_id] is None or remaining_deps[task_id] in resolved_set
                ),
                key=sort_key,
            )
            if not ready:
                raise CyclicDependencyError(
                    f"Cyclic or unresolved dependency chain among tasks: {sorted(pending)}"
                )
            for task_id in ready:
                resolved.append(task_id)
                resolved_set.add(task_id)
                pending.discard(task_id)

        return resolved

    def group_batches(self, ordered_task_ids: list[str], batch_size: int) -> list[list[str]]:
        """Split an already-ordered id list into fixed-size batches,
        preserving order within and across batches."""
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        return [ordered_task_ids[i : i + batch_size] for i in range(0, len(ordered_task_ids), batch_size)]
