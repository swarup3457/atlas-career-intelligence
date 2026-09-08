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


class CoveragePlanState(str, enum.Enum):
    """Lifecycle of a coverage plan (build spec 7.8). A plan is not
    production-executable until it is SEALED; a FAILED plan is explicitly
    distinct from a legitimately empty ``NO_WORK_DUE`` plan."""

    BUILDING = "BUILDING"
    SEALED = "SEALED"
    FAILED = "FAILED"


class EmptyPlanError(RuntimeError):
    """Raised when sealing an empty plan without explicitly allowing it —
    an empty plan must never silently look like successful completion."""


class DuplicateCoverageError(ValueError):
    """Raised when a coverage_id is re-planned with a DIFFERENT plan identity
    (a silent overwrite of a different task). Idempotent re-planning of the
    same identity is allowed."""


class PlanNotSealedError(RuntimeError):
    """Raised when an unsealed/failed plan is used where a sealed production
    plan is required."""


class TerminalPlanState(str, enum.Enum):
    """Truthful terminal classification of a whole plan."""

    BUILDING = "BUILDING"          # not sealed yet — never 'complete'
    FAILED = "FAILED"              # planning failed
    NO_WORK_DUE = "NO_WORK_DUE"    # sealed, legitimately empty
    IN_PROGRESS = "IN_PROGRESS"    # sealed, some tasks not terminal
    COMPLETE = "COMPLETE"          # sealed, has tasks, all terminal


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
    # Terminal, but HONESTLY partial: a configured hard page/cursor budget was
    # reached with more results still available. This is NOT full-board coverage
    # and must never be presented as a COMPLETED_WITH_RESULTS proof (build spec
    # 10/20). It is terminal so a bounded canary plan can still complete.
    PARTIAL_BUDGET = "PARTIAL_BUDGET"
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
        CoverageStatus.PARTIAL_BUDGET,
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

    @property
    def plan_key(self) -> tuple:
        """The stable *planned identity* of this task (what it covers), used
        to detect a conflicting silent overwrite of a different task under
        the same coverage_id. Runtime fields (status, counts) are excluded."""
        return (
            self.source_instance,
            self.company,
            self.source_type,
            self.lane,
            self.query_key,
        )

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
    """A run's planned coverage. Deterministic ordering by coverage_id.

    Phase 1B (build spec 7.8) adds a plan lifecycle so a plan cannot
    silently empty-complete, a planning failure is distinct from
    ``NO_WORK_DUE``, and a conflicting duplicate coverage_id is rejected.
    """

    def __init__(self, run_id: str):
        self.run_id = run_id
        self._tasks: dict[str, CoverageTask] = {}
        self.state: CoveragePlanState = CoveragePlanState.BUILDING
        self.no_work_due: bool = False
        self.failure_reason: Optional[str] = None
        self._sealed_fingerprint: Optional[str] = None
        self.policy_fingerprint: Optional[str] = None

    def plan(self, task: CoverageTask) -> CoverageTask:
        existing = self._tasks.get(task.coverage_id)
        if existing is not None and existing.plan_key != task.plan_key:
            raise DuplicateCoverageError(
                f"coverage_id {task.coverage_id!r} already planned with a different "
                f"identity {existing.plan_key} != {task.plan_key}"
            )
        if self.state == CoveragePlanState.SEALED and existing is None:
            raise PlanNotSealedError(
                f"cannot add new task {task.coverage_id!r} to a SEALED plan"
            )
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
        """A plan is complete only when it actually has tasks and all are
        terminal, OR it was explicitly sealed as ``NO_WORK_DUE``. An empty,
        unsealed plan is NEVER complete (fixes the silent empty-complete
        bug, build spec 7.8)."""
        if self.state == CoveragePlanState.FAILED:
            return False
        if not self._tasks:
            return self.state == CoveragePlanState.SEALED and self.no_work_due
        return all(t.is_terminal for t in self._tasks.values())

    # -- plan lifecycle (build spec 7.8) -----------------------------------
    def fingerprint(self) -> str:
        """Deterministic sha256 over the sorted planned identities. Stable
        across runs given the same plan; changes if the plan changes."""
        import hashlib
        import json

        payload = [
            {
                "coverage_id": t.coverage_id,
                "source_instance": t.source_instance,
                "company": t.company,
                "source_type": t.source_type,
                "lane": t.lane,
                "query_key": t.query_key,
            }
            for t in self.tasks()
        ]
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def seal(self, *, allow_empty: bool = False) -> str:
        """Freeze the plan for execution. Returns the plan fingerprint.

        An empty plan may only be sealed with ``allow_empty=True`` and is
        then marked ``NO_WORK_DUE`` — never silently 'complete'."""
        if self.state == CoveragePlanState.FAILED:
            raise PlanNotSealedError("cannot seal a FAILED plan")
        if not self._tasks:
            if not allow_empty:
                raise EmptyPlanError(
                    "refusing to seal an empty plan; pass allow_empty=True to record NO_WORK_DUE"
                )
            self.no_work_due = True
        self.state = CoveragePlanState.SEALED
        self._sealed_fingerprint = self.fingerprint()
        return self._sealed_fingerprint

    def mark_failed(self, reason: str) -> None:
        """Record a planning failure — explicitly distinct from NO_WORK_DUE."""
        self.state = CoveragePlanState.FAILED
        self.failure_reason = reason

    @property
    def is_sealed(self) -> bool:
        return self.state == CoveragePlanState.SEALED

    @property
    def sealed_fingerprint(self) -> Optional[str]:
        return self._sealed_fingerprint

    def is_executable(self) -> bool:
        """Only a SEALED plan may execute as production."""
        return self.state == CoveragePlanState.SEALED

    def require_sealed(self) -> None:
        if self.state != CoveragePlanState.SEALED:
            raise PlanNotSealedError(
                f"plan {self.run_id!r} is {self.state.value}, not SEALED; cannot execute as production"
            )

    def terminal_state(self) -> TerminalPlanState:
        """Truthful terminal classification of the whole plan."""
        if self.state == CoveragePlanState.FAILED:
            return TerminalPlanState.FAILED
        if self.state != CoveragePlanState.SEALED:
            return TerminalPlanState.BUILDING
        if not self._tasks:
            return TerminalPlanState.NO_WORK_DUE
        if all(t.is_terminal for t in self._tasks.values()):
            return TerminalPlanState.COMPLETE
        return TerminalPlanState.IN_PROGRESS

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for t in self._tasks.values():
            counts[t.status.value] = counts.get(t.status.value, 0) + 1
        counts["_planned"] = len(self._tasks)
        counts["_terminal"] = sum(1 for t in self._tasks.values() if t.is_terminal)
        return counts

    def lane_summary(self) -> dict[str, dict[str, int]]:
        """Per-lane planned vs terminal accounting (build spec 8 / P0-11). Each
        required lane is independently accountable, so a failed lane can never
        be hidden by a successful sibling lane in the same parent batch."""
        out: dict[str, dict[str, int]] = {}
        for t in self._tasks.values():
            lane = t.lane or "ALL"
            bucket = out.setdefault(lane, {"planned": 0, "terminal": 0})
            bucket["planned"] += 1
            if t.is_terminal:
                bucket["terminal"] += 1
        return out

    # -- persistence --------------------------------------------------------
    def persist(self, store, *, policy_fingerprint: Optional[str] = None) -> None:
        """Persist BOTH the plan lifecycle row (state/no_work_due/failure_
        reason/sealed+policy fingerprints) AND every child task identity/status,
        so a fresh process can round-trip the full sealed plan (build spec 7 /
        P1-1). Call this BEFORE discovery so a sealed plan is durable."""
        if policy_fingerprint is not None:
            self.policy_fingerprint = policy_fingerprint
        store.upsert_coverage_plan(
            self.run_id,
            self.state.value,
            no_work_due=self.no_work_due,
            fingerprint=self._sealed_fingerprint,
            policy_fingerprint=self.policy_fingerprint,
            failure_reason=self.failure_reason,
        )
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
        # Restore the plan lifecycle (state/no_work_due/failure_reason/sealed +
        # policy fingerprints) so a resumed plan is byte-for-byte the same sealed
        # plan — not a fresh BUILDING plan (build spec 7 / P1-1).
        plan_row = store.get_coverage_plan(run_id)
        if plan_row is not None:
            try:
                manifest.state = CoveragePlanState(plan_row["state"])
            except (ValueError, KeyError):
                manifest.state = CoveragePlanState.BUILDING
            manifest.no_work_due = bool(plan_row["no_work_due"])
            manifest.failure_reason = plan_row["failure_reason"]
            manifest._sealed_fingerprint = plan_row["fingerprint"]
            manifest.policy_fingerprint = plan_row["policy_fingerprint"]
        return manifest


__all__ = [
    "CoverageStatus",
    "TERMINAL_COVERAGE_STATUSES",
    "coverage_status_for",
    "CoverageTask",
    "CoverageManifest",
    "CoveragePlanState",
    "TerminalPlanState",
    "EmptyPlanError",
    "DuplicateCoverageError",
    "PlanNotSealedError",
]
