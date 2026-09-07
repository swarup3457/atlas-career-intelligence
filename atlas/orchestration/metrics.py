"""Atlas generic run metrics (no business-specific metrics yet).

Deliberately generic - counts of planned/attempted/completed/remaining
tasks, retries, access-limited, failures, and elapsed time. Job-specific
business metrics (e.g. "jobs found") will be added once the Workspace
Atlas Agent specification is imported.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from atlas.orchestration.events import Event, EventType


@dataclass
class RunMetrics:
    planned_tasks: int = 0
    attempted_tasks: int = 0
    completed_tasks: int = 0
    remaining_tasks: int = 0
    retry_count: int = 0
    access_limited_count: int = 0
    failed_count: int = 0
    _started_at: float = field(default_factory=time.monotonic, repr=False)
    _elapsed_seconds: float = 0.0

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._started_at

    def to_dict(self) -> dict[str, float | int]:
        return {
            "planned_tasks": self.planned_tasks,
            "attempted_tasks": self.attempted_tasks,
            "completed_tasks": self.completed_tasks,
            "remaining_tasks": self.remaining_tasks,
            "retry_count": self.retry_count,
            "access_limited_count": self.access_limited_count,
            "failed_count": self.failed_count,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }

    def observe_event(self, event: Event) -> None:
        """Update counters from a published Atlas runtime Event - keeps
        metrics collection decoupled from orchestration internals; any
        code that publishes events automatically feeds metrics if this
        method is wired as an EventBus subscriber."""
        if event.event_type == EventType.TASK_STARTED:
            self.attempted_tasks += 1
        elif event.event_type == EventType.TASK_RETRY:
            self.retry_count += 1
        elif event.event_type == EventType.TASK_COMPLETED:
            self.completed_tasks += 1
        elif event.event_type == EventType.TASK_FAILED:
            self.failed_count += 1
        elif event.event_type == EventType.ACCESS_LIMITED:
            self.access_limited_count += 1


def metrics_from_queue_state(state: dict) -> RunMetrics:
    """Convenience constructor: derive a RunMetrics snapshot from an
    atlas.orchestration.state.QueueState-shaped dict (works for the
    generic single-node governor without requiring event-bus wiring)."""
    metrics = RunMetrics()
    metrics.planned_tasks = len(state.get("planned_items", []))
    metrics.completed_tasks = len(state.get("completed_items", []))
    metrics.remaining_tasks = len(state.get("remaining_items", []))
    metrics.attempted_tasks = len(state.get("attempt_log", []))
    metrics.retry_count = sum(max(0, v - 1) for v in state.get("retry_counts", {}).values())
    metrics.access_limited_count = len(state.get("access_limited_items", []))
    metrics.failed_count = len(state.get("failed_items", []))
    return metrics
