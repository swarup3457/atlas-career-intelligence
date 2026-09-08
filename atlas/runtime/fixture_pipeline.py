"""Common production execution path for fixture (and real) adapters
(Phase 1B.1, build spec 4 / P0-1; Phase 1C-A refactor).

This is the ONE sequential path a sealed coverage plan is executed through — the
same path real adapters use — so nothing here is fixture-specific except the
adapters it drives:

    sealed plan child  -> SourceInstance -> SourceRegistry.create(adapter)
      -> shared RateLimitedExecutor -> SourceSearchWorker / typed WorkerOutcome
      -> centralized retry policy (atlas.orchestration.retry)
      -> append-only coverage attempt record
      -> source health/yield observation keyed by QuerySignature
      -> raw discovery observation staging (NOT canonical jobs)
      -> coverage child terminal/human-blocked status update

The per-child work now lives in the shared
:class:`atlas.sources.child_executor.CoverageChildExecutor` so the bounded
parallel dispatcher runs the EXACT same code per child — concurrency 1 and
concurrency N therefore produce identical canonical results. There is NO second
fake-only orchestration stack: FakeAdapter/FixtureAdapter are executed exactly
as a real adapter would be; one adapter call == one attempt; retry authority
stays centralized; every planned child ends in a durable state.

``canonical_dedupe_key`` is re-exported here for backward compatibility; it now
lives in :mod:`atlas.sources.child_executor`.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Mapping, Optional

from atlas.sources.child_executor import CoverageChildExecutor, canonical_dedupe_key
from atlas.sources.coverage import CoverageManifest, CoverageStatus, CoverageTask
from atlas.sources.executor import RateLimitedExecutor
from atlas.sources.models import SourceInstance
from atlas.sources.registry import SourceRegistry


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@dataclass
class TaskExecutionResult:
    coverage_id: str
    lane: Optional[str]
    status: CoverageStatus
    attempts: int
    observations_staged: int
    sentinel_ran: bool = False


@dataclass
class PipelineResult:
    executed: int = 0
    attempts: int = 0
    observations_staged: int = 0
    sentinels_run: int = 0
    per_task: list[TaskExecutionResult] = field(default_factory=list)


class FixtureExecutionPipeline:
    """Executes pending coverage children SEQUENTIALLY through the real source
    pipeline (delegating each child to the shared CoverageChildExecutor)."""

    def __init__(
        self,
        store,
        registry: SourceRegistry,
        instances: Mapping[str, SourceInstance],
        *,
        executor: Optional[RateLimitedExecutor] = None,
        run_id: str,
        policy_version: str = "unversioned",
        retry_budget: int = 2,
        limit: int = 25,
        query_compiler=None,
        geography=None,
        snapshot_cache=None,
        max_pages: int = 50,
    ):
        self.store = store
        self.registry = registry
        self.instances = dict(instances)
        self.executor = executor or RateLimitedExecutor()
        self.run_id = run_id
        self.policy_version = policy_version
        self.retry_budget = retry_budget
        self.limit = limit
        self._child = CoverageChildExecutor(
            store, registry, instances, executor=self.executor, run_id=run_id,
            policy_version=policy_version, retry_budget=retry_budget, limit=limit,
            query_compiler=query_compiler, geography=geography, snapshot_cache=snapshot_cache,
            max_pages=max_pages,
        )

    # -- execution ----------------------------------------------------------
    def execute(
        self, manifest: CoverageManifest, coverage_ids: list[str], *, max_children: Optional[int] = None
    ) -> PipelineResult:
        result = PipelineResult()
        for coverage_id in coverage_ids:
            if max_children is not None and result.executed >= max_children:
                break
            task = manifest.get(coverage_id)
            if task is None or task.is_terminal or task.status == CoverageStatus.BLOCKED_HUMAN:
                continue
            if task.source_instance not in self.instances:
                # A pseudo-instance (discovery/portal/ats placeholder) has no
                # adapter in this run: mark it truthfully as an unresolved
                # extraction rather than pretending it completed.
                manifest.mark(coverage_id, CoverageStatus.EXTRACTION_UNRESOLVED,
                              next_action="NONE", detail={"reason": "no adapter for instance"})
                self._persist_task(manifest, coverage_id)
                task_res = TaskExecutionResult(coverage_id, task.lane, CoverageStatus.EXTRACTION_UNRESOLVED, 0, 0)
                result.per_task.append(task_res)
                result.executed += 1
                continue
            task_res = self._execute_one(manifest, task)
            # Persist this child IMMEDIATELY so a crash mid-DISCOVER leaves a
            # durable partial subset (build spec 6) — the next process resumes
            # the exact remaining children without repeating this one.
            self._persist_task(manifest, coverage_id)
            result.per_task.append(task_res)
            result.executed += 1
            result.attempts += task_res.attempts
            result.observations_staged += task_res.observations_staged
            if task_res.sentinel_ran:
                result.sentinels_run += 1
        return result

    def _persist_task(self, manifest: CoverageManifest, coverage_id: str) -> None:
        task = manifest.get(coverage_id)
        if task is None:
            return
        self.store.upsert_coverage(
            task.coverage_id, self.run_id, task.source_instance,
            company=task.company, source_type=task.source_type, lane=task.lane,
            query_key=task.query_key, attempted=task.attempted, completed=task.completed,
            status=task.status.value, jobs_found=task.jobs_found, new_jobs=task.new_jobs,
            changed_jobs=task.changed_jobs, closed_jobs=task.closed_jobs,
            started_at=task.started_at, completed_at=task.completed_at,
            next_action=task.next_action, detail=task.detail,
        )

    def _execute_one(self, manifest: CoverageManifest, task: CoverageTask) -> TaskExecutionResult:
        manifest.mark(task.coverage_id, CoverageStatus.IN_PROGRESS, started_at=_utcnow(), next_action="SEARCH")
        outcome = self._child.execute(task)
        manifest.mark(
            task.coverage_id, outcome.status, jobs_found=outcome.jobs_found,
            started_at=outcome.started_at, completed_at=outcome.completed_at,
            next_action=outcome.next_action, detail=outcome.detail,
        )
        return TaskExecutionResult(
            outcome.coverage_id, outcome.lane, outcome.status,
            outcome.attempts, outcome.observations_staged, outcome.sentinel_ran,
        )


__all__ = [
    "FixtureExecutionPipeline",
    "PipelineResult",
    "TaskExecutionResult",
    "canonical_dedupe_key",
]
