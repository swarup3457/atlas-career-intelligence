"""Atlas machine-readable progress snapshot (Phase 0.75 spec section 13).

Every number here is derived purely from durable QueueState /
StateStore-shaped facts - never computed or estimated by an LLM.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

from atlas.models import TaskStatus
from atlas.runtime.states import RunState


@dataclass
class ProgressSnapshot:
    run_id: str
    status: str
    planned: int
    completed: int
    remaining: int
    retry_pending: int
    access_limited: int
    extraction_unresolved: int
    waiting_for_human: int
    permanent_failure: int
    success: int

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def compute_progress(run_id: str, run_state: RunState, queue_state: dict) -> ProgressSnapshot:
    """Derive a ProgressSnapshot from a QueueState-shaped dict (see
    atlas.orchestration.state.QueueState) plus the runtime's own RunState.
    """
    item_results: dict[str, dict[str, Any]] = queue_state.get("item_results", {})
    retry_counts: dict[str, int] = queue_state.get("retry_counts", {})
    planned = len(queue_state.get("planned_items", []))
    completed = len(queue_state.get("completed_items", []))
    remaining = len(queue_state.get("remaining_items", []))

    def _count(status: TaskStatus) -> int:
        return sum(1 for r in item_results.values() if r.get("status") == status.value)

    # An item is "retry pending" if it is still in remaining_items AND has
    # already been attempted at least once (retry_counts entry > 0).
    remaining_items = set(queue_state.get("remaining_items", []))
    retry_pending = sum(1 for item in remaining_items if retry_counts.get(item, 0) > 0)

    return ProgressSnapshot(
        run_id=run_id,
        status=run_state.value if isinstance(run_state, RunState) else str(run_state),
        planned=planned,
        completed=completed,
        remaining=remaining,
        retry_pending=retry_pending,
        access_limited=_count(TaskStatus.ACCESS_LIMITED),
        extraction_unresolved=_count(TaskStatus.EXTRACTION_UNRESOLVED),
        waiting_for_human=_count(TaskStatus.WAITING_FOR_HUMAN) + _count(TaskStatus.LOGIN_REQUIRED),
        permanent_failure=_count(TaskStatus.PERMANENT_FAILURE),
        success=_count(TaskStatus.SUCCESS),
    )
