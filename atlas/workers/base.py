"""Atlas worker base classes.

A Worker performs ONE attempt of ONE queue item and returns a
:class:`WorkerOutcome`. It never decides retry policy itself — that is
centralized in atlas/orchestration/retry.py and applied by the LangGraph
governor node built in atlas/orchestration/graph.py. This keeps "what
remains / what completed / retries" entirely inside deterministic
orchestration code, never inside worker or LLM logic.

Phase 0.5 formalizes the FULL typed result contract
(:class:`TaskResult`) that the governor assembles from a worker's
WorkerOutcome/WorkerError plus its own bookkeeping (task id, timing,
attempt number). This is the shape future career/ATS/portal/verification
workers' results will always take — completion is communicated only
through these typed fields, never through free-form text.
"""

from __future__ import annotations

import dataclasses
import datetime
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from atlas.models import ErrorCategory, TaskStatus


class WorkerError(Exception):
    """Raised by a worker when an attempt fails, classified so the
    centralized retry policy can decide what happens next."""

    def __init__(self, category: ErrorCategory, message: str):
        super().__init__(message)
        self.category = category
        self.message = message


@dataclass
class WorkerOutcome:
    """Result of a single successful worker attempt (no exception raised).

    status must be one of the "successful/neutral terminal" statuses
    (SUCCESS, NO_RELEVANT_RESULTS, EXTRACTION_UNRESOLVED, SKIPPED, CLOSED)
    — failure paths are represented by raising WorkerError instead, so the
    governor can apply centralized retry policy uniformly.
    """

    status: TaskStatus
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskResult:
    """The FULL formal, typed result contract for one processed task.

    Assembled by the governor (atlas/orchestration/graph.py), never by
    the worker directly, since only the governor knows timing/attempt
    bookkeeping. This is what "a worker must return a typed result ...
    never communicate completion merely through free-form text" means in
    practice: every terminal outcome recorded by Atlas has exactly this
    shape.
    """

    task_id: str
    status: str  # atlas.models.TaskStatus value
    started_at: str
    completed_at: str
    attempt: int
    data: dict[str, Any] = field(default_factory=dict)
    error_category: Optional[str] = None
    error_detail: Optional[str] = None
    next_action: str = "NONE"  # e.g. "NONE", "RETRY", "ESCALATE", "REQUEUE_ALTERNATE_STRATEGY"

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def make_task_result(
    task_id: str,
    status: TaskStatus,
    started_at: str,
    attempt: int,
    data: Optional[dict[str, Any]] = None,
    error_category: Optional[ErrorCategory] = None,
    error_detail: Optional[str] = None,
    next_action: str = "NONE",
) -> TaskResult:
    """Convenience constructor used by the governor to build a TaskResult
    with a completed_at timestamp of "now"."""
    return TaskResult(
        task_id=task_id,
        status=status.value if isinstance(status, TaskStatus) else str(status),
        started_at=started_at,
        completed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        attempt=attempt,
        data=data or {},
        error_category=error_category.value if isinstance(error_category, ErrorCategory) else error_category,
        error_detail=error_detail,
        next_action=next_action,
    )


class BaseWorker(ABC):
    """Abstract base for all Atlas workers (career-page checks, ATS
    lookups, portal checks, verification, etc.)."""

    name: str = "base"

    @abstractmethod
    def attempt(self, item: str, attempt_number: int) -> WorkerOutcome:
        """Perform exactly one attempt at processing `item`.

        Must either:
            - return a WorkerOutcome with a successful/neutral terminal
              status, or
            - raise WorkerError with an ErrorCategory so the governor's
              centralized retry policy can decide retry vs. escalate.

        Must NOT itself implement retry loops, sleep-and-retry, or decide
        permanent failure — that is the governor's job.
        """
        raise NotImplementedError

