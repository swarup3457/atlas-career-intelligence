"""Atlas search coverage manifest (Phase 1A).

Completion is defined as "every planned task reached a terminal state" —
NOT "we found N jobs". This module models the planned unit of work
(company × source instance × lane × query / detail check) and tracks each
one's terminal coverage status, so a run can truthfully report

    NOT_ATTEMPTED  vs  ATTEMPTED_ZERO  vs  COMPLETED_WITH_RESULTS  vs
    FAILED  vs  ACCESS_LIMITED  vs  EXTRACTION_UNRESOLVED (and rate/unavailable)

instead of silently concluding "searched 12 companies and stopped".

The manifest can be persisted to / reloaded from the StateStore
``coverage_records`` table so a run resumes exactly where it left off.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Iterable, Optional

from atlas.models import TaskStatus
from atlas.sources.models import ZeroResultKind


class CoverageStatus(str, enum.Enum):
    # Not yet run.
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    IN_PROGRESS = "IN_PROGRESS"
    # Terminal outcomes.
    COMPLETED_WITH_RESULTS = "COMPLETED_WITH_RESULTS"
    ATTEMPTED_ZERO = "ATTEMPTED_ZERO"
    EXTRACTION_UNRESOLVED = "EXTRACTION_UNRESOLVED"
    ACCESS_LIMITED = "ACCESS_LIMITED"
    RATE_LIMITED = "RATE_LIMITED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    FAILED = "FAILED"
    # Blocked awaiting a human (non-terminal for coverage).
    BLOCKED_HUMAN = "BLOCKED_HUMAN"


TERMINAL_COVERAGE_STATUSES: frozenset[CoverageStatus] = frozenset(
    {
        CoverageStatus.COMPLETED_WITH_RESULTS,
        CoverageStatus.ATTEMPTED_ZERO,
        CoverageStatus.EXTRACTION_UNRESOLVED,
        CoverageStatus.ACCESS_LIMITED,
        CoverageStatus.RATE_LIMITED,
        CoverageStatus.SOURCE_UNAVAILABLE,
        CoverageStatus.FAILED,
    }
)


def coverage_status_for(
    task_status: TaskStatus,
    *,
    result_count: int = 0,
    zero_kind: Optional[ZeroResultKind] = None,
) -> CoverageStatus:
    """Deterministically map a task's terminal :class:`TaskStatus` (plus how
    many results it yielded and any zero-result classification) to a
    coverage status."""
    if task_status == TaskStatus.SUCCESS:
        return (
            CoverageStatus.COMPLETED_WITH_RESULTS
            if result_count > 0
            else CoverageStatus.ATTEMPTED_ZERO
        )
    if task_status == TaskStatus.NO_RELEVANT_RESULTS:
        if zero_kind == ZeroResultKind.EXTRACTION_UNRESOLVED:
            return CoverageStatus.EXTRACTION_UNRESOLVED
        return CoverageStatus.ATTEMPTED_ZERO
    if task_status == TaskStatus.EXTRACTION_UNRESOLVED:
        return CoverageStatus.EXTRACTION_UNRESOLVED
    if task_status == TaskStatus.ACCESS_LIMITED:
        return CoverageStatus.ACCESS_LIMITED
    if task_status == TaskStatus.RATE_LIMITED:
        return CoverageStatus.RATE_LIMITED
    if task_status == TaskStatus.SOURCE_UNAVAILABLE:
        return CoverageStatus.SOURCE_UNAVAILABLE
    if task_status in (TaskStatus.LOGIN_REQUIRED, TaskStatus.WAITING_FOR_HUMAN):
        return CoverageStatus.BLOCKED_HUMAN
    if task_status in (TaskStatus.PERMANENT_FAILURE, TaskStatus.CLOSED, TaskStatus.SKIPPED):
        return CoverageStatus.FAILED if task_status == TaskStatus.PERMANENT_FAILURE else CoverageStatus.ATTEMPTED_ZERO
    return CoverageStatus.IN_PROGRESS


@dataclass
class CoverageTask:
    coverage_id: str
    source_instance: str
    company: Optional[str] = None
    source_type: Optional[str] = None
    lane: Optional[str] = None
    query_key: Optional[str] = None
    status: CoverageStatus = CoverageStatus.NOT_ATTEMPTED
    attempted: bool = False
    completed: bool = False
    jobs_found: int = 0
    new_jobs: int = 0
    changed_jobs: int = 0
    closed_jobs: int = 0
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    next_action: str = "NONE"
    detail: dict = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_COVERAGE_STATUSES

    def to_dict(self) -> dict:
        return {
            "coverage_id": self.coverage_id,
            "source_instance": self.source_instance,
            "company": self.company,
            "source_type": self.source_type,
            "lane": self.lane,
            "query_key": self.query_key,
            "status": self.status.value,
            "attempted": self.attempted,
            "completed": self.completed,
            "jobs_found": self.jobs_found,
            "new_jobs": self.new_jobs,
            "changed_jobs": self.changed_jobs,
            "closed_jobs": self.closed_jobs,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "next_action": self.next_action,
            "detail": dict(self.detail),
        }


class CoverageManifest:
    """A run's planned coverage. Deterministic ordering by coverage_id."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self._tasks: dict[str, CoverageTask] = {}

    def plan(self, task: CoverageTask) -> CoverageTask:
        self._tasks[task.coverage_id] = task
        return task

    def plan_many(self, tasks: Iterable[CoverageTask]) -> None:
        for t in tasks:
            self.plan(t)

    def get(self, coverage_id: str) -> Optional[CoverageTask]:
        return self._tasks.get(coverage_id)

    def mark(
        self,
        coverage_id: str,
        status: CoverageStatus,
        *,
        jobs_found: int = 0,
        new_jobs: int = 0,
        changed_jobs: int = 0,
        closed_jobs: int = 0,
        started_at: Optional[str] = None,
        completed_at: Optional[str] = None,
        next_action: str = "NONE",
        detail: Optional[dict] = None,
    ) -> CoverageTask:
        task = self._tasks[coverage_id]
        task.status = status
        task.attempted = True
        task.completed = status in TERMINAL_COVERAGE_STATUSES
        task.jobs_found = jobs_found
        task.new_jobs = new_jobs
        task.changed_jobs = changed_jobs
        task.closed_jobs = closed_jobs
        if started_at is not None:
            task.started_at = started_at
        if completed_at is not None:
            task.completed_at = completed_at
        task.next_action = next_action
        if detail is not None:
            task.detail = detail
        return task

    def tasks(self) -> list[CoverageTask]:
        return [self._tasks[k] for k in sorted(self._tasks)]

    def remaining(self) -> list[CoverageTask]:
        return [t for t in self.tasks() if not t.is_terminal]

    def is_complete(self) -> bool:
        return all(t.is_terminal for t in self._tasks.values()) if self._tasks else True

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for t in self._tasks.values():
            counts[t.status.value] = counts.get(t.status.value, 0) + 1
        counts["_planned"] = len(self._tasks)
        counts["_terminal"] = sum(1 for t in self._tasks.values() if t.is_terminal)
        return counts

    # -- persistence --------------------------------------------------------
    def persist(self, store) -> None:
        for t in self.tasks():
            store.upsert_coverage(
                t.coverage_id,
                self.run_id,
                t.source_instance,
                company=t.company,
                source_type=t.source_type,
                lane=t.lane,
                query_key=t.query_key,
                attempted=t.attempted,
                completed=t.completed,
                status=t.status.value,
                jobs_found=t.jobs_found,
                new_jobs=t.new_jobs,
                changed_jobs=t.changed_jobs,
                closed_jobs=t.closed_jobs,
                started_at=t.started_at,
                completed_at=t.completed_at,
                next_action=t.next_action,
                detail=t.detail,
            )

    @classmethod
    def load(cls, store, run_id: str) -> "CoverageManifest":
        manifest = cls(run_id)
        for row in store.list_coverage(run_id):
            try:
                status = CoverageStatus(row["status"])
            except ValueError:
                status = CoverageStatus.NOT_ATTEMPTED
            manifest.plan(
                CoverageTask(
                    coverage_id=row["coverage_id"],
                    source_instance=row["source_instance"],
                    company=row["company"],
                    source_type=row["source_type"],
                    lane=row["lane"],
                    query_key=row["query_key"],
                    status=status,
                    attempted=bool(row["attempted"]),
                    completed=bool(row["completed"]),
                    jobs_found=int(row["jobs_found"]),
                    new_jobs=int(row["new_jobs"]),
                    changed_jobs=int(row["changed_jobs"]),
                    closed_jobs=int(row["closed_jobs"]),
                    started_at=row["started_at"],
                    completed_at=row["completed_at"],
                    next_action=row["next_action"],
                )
            )
        return manifest


__all__ = [
    "CoverageStatus",
    "TERMINAL_COVERAGE_STATUSES",
    "coverage_status_for",
    "CoverageTask",
    "CoverageManifest",
]
