"""Common production execution path for fixture (and later real) adapters
(Phase 1B.1, build spec 4 / P0-1).

This is the ONE path a sealed coverage plan is executed through — the same path
real adapters will use in Phase 1C, so nothing here is fixture-specific except
the adapters it drives:

    sealed plan child  -> SourceInstance -> SourceRegistry.create(adapter)
      -> shared RateLimitedExecutor -> SourceSearchWorker / typed WorkerOutcome
      -> centralized retry policy (atlas.orchestration.retry)
      -> append-only coverage attempt record
      -> source health/yield observation keyed by QuerySignature
      -> raw discovery observation staging (NOT canonical jobs)
      -> coverage child terminal/human-blocked status update

There is NO second fake-only orchestration stack: FakeAdapter/FixtureAdapter
are executed exactly as a real adapter would be. One adapter call == one
attempt; retry authority stays centralized; every planned child ends in a
durable state.
"""

from __future__ import annotations

import datetime
import hashlib
from dataclasses import dataclass, field
from typing import Mapping, Optional

from atlas.models import ErrorCategory, TaskStatus
from atlas.orchestration import retry
from atlas.sources.coverage import (
    CoverageManifest,
    CoverageStatus,
    CoverageTask,
    coverage_status_for,
)
from atlas.sources.executor import RateLimitedExecutor
from atlas.sources.models import SearchRequest, SourceInstance
from atlas.sources.query_signature import QuerySignature
from atlas.sources.registry import SourceRegistry
from atlas.sources.worker import SourceSearchWorker, SourceTask
from atlas.workers.base import WorkerError


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def canonical_dedupe_key(
    company: Optional[str], title: Optional[str], location: Optional[str],
    posted_at: Optional[str], url: Optional[str],
) -> str:
    """Source-INDEPENDENT dedupe key. Cross-source duplicates of the SAME
    posting (same company/title/location/date) collapse to one canonical job;
    a probable repost (same role, DIFFERENT posted date) keeps a distinct key
    and is never silently merged (build spec 12)."""
    parts = [
        " ".join(str(company or "").lower().split()),
        " ".join(str(title or "").lower().split()),
        " ".join(str(location or "").lower().split()),
        str(posted_at or ""),
        str(url or ""),
    ]
    return hashlib.sha256("||".join(parts).encode("utf-8")).hexdigest()


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


# Health-state label derived from the terminal task status (diagnostic only).
_STATUS_HEALTH = {
    TaskStatus.SUCCESS: "HEALTHY",
    TaskStatus.NO_RELEVANT_RESULTS: "HEALTHY",
    TaskStatus.EXTRACTION_UNRESOLVED: "SELECTOR_DRIFT_SUSPECTED",
    TaskStatus.RATE_LIMITED: "RATE_LIMITED",
    TaskStatus.SOURCE_UNAVAILABLE: "SOURCE_UNAVAILABLE",
    TaskStatus.ACCESS_LIMITED: "ACCESS_LIMITED",
    TaskStatus.LOGIN_REQUIRED: "AUTH_REQUIRED",
    TaskStatus.WAITING_FOR_HUMAN: "AUTH_REQUIRED",
}


class FixtureExecutionPipeline:
    """Executes pending coverage children through the real source pipeline."""

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
    ):
        self.store = store
        self.registry = registry
        self.instances = dict(instances)
        self.executor = executor or RateLimitedExecutor()
        self.run_id = run_id
        self.policy_version = policy_version
        self.retry_budget = retry_budget
        self.limit = limit
        self._adapters: dict[str, object] = {}

    # -- helpers ------------------------------------------------------------
    def _adapter_for(self, instance_id: str):
        if instance_id not in self._adapters:
            self._adapters[instance_id] = self.registry.create(self.instances[instance_id])
        return self._adapters[instance_id]

    def _history_for_signature(self, fingerprint: str):
        return self.store.recent_yields_for_signature(fingerprint)

    def _build_source_task(self, task: CoverageTask) -> tuple[SourceTask, QuerySignature]:
        instance = self.instances[task.source_instance]
        geo = task.query_key or "PRIMARY"
        sig = QuerySignature.for_instance(
            instance, lane=task.lane, geography_group=geo, search_mode="DELTA",
            keywords=[task.lane] if task.lane else None, policy_version=self.policy_version,
        )
        request = SearchRequest(query=task.lane or "software", location=geo, company=task.company, limit=self.limit)
        sentinel = SearchRequest(
            query=task.lane or "software", location=geo, company=task.company,
            limit=self.limit, extra_filters={"sentinel": True},
        )
        return (
            SourceTask(
                item_id=task.coverage_id, instance_id=task.source_instance, request=request,
                lane=task.lane, company=task.company, sentinel_request=sentinel, query_signature=sig,
            ),
            sig,
        )

    def _stage_observations(self, task: CoverageTask, sig: QuerySignature, attempt_id: str, payload: dict) -> int:
        staged = 0
        for i, raw in enumerate(payload.get("results", []) or []):
            source_identity = raw.get("source_job_id") or raw.get("canonical_url") or raw.get("source_url") or f"{task.coverage_id}#{i}"
            observation_id = f"obs::{self.run_id}::{task.coverage_id}::{source_identity}"
            dedupe = canonical_dedupe_key(
                raw.get("company"), raw.get("title"), raw.get("location"),
                raw.get("posted_at"), raw.get("canonical_url") or raw.get("source_url"),
            )
            inserted = self.store.stage_raw_observation(
                observation_id, self.run_id, task.source_instance, dedupe,
                coverage_id=task.coverage_id, attempt_id=attempt_id,
                query_signature=sig.fingerprint(),
                source_family=self.instances[task.source_instance].adapter_key.value,
                source_job_id=raw.get("source_job_id"), source_url=raw.get("source_url"),
                canonical_url=raw.get("canonical_url"), company=raw.get("company"),
                title=raw.get("title"), location=raw.get("location"), lane=task.lane,
                posted_at=raw.get("posted_at"), is_active=str(raw.get("is_active", "")),
                source_identity=f"{task.source_instance}::{source_identity}",
                adapter_version=raw.get("adapter_version", ""), parser_version=raw.get("parser_version", ""),
            )
            if inserted:
                staged += 1
        return staged

    def _record_attempt(self, task: CoverageTask, sig: QuerySignature, attempt_number: int,
                        status: str, *, jobs_found: int = 0, detail: Optional[dict] = None) -> str:
        attempt_id = f"att::{self.run_id}::{task.coverage_id}::{attempt_number}"
        self.store.append_coverage_attempt(
            attempt_id, task.coverage_id, self.run_id, task.source_instance, status,
            query_signature=sig.fingerprint(), jobs_found=jobs_found, detail=detail or {},
        )
        return attempt_id

    def _record_health(self, task: CoverageTask, sig: QuerySignature, attempt_number: int,
                       state: str, result_count: Optional[int]) -> None:
        self.store.record_source_health(
            f"health::{self.run_id}::{task.coverage_id}::{attempt_number}",
            task.source_instance, state, result_count=result_count,
            query_signature=sig.fingerprint(), lane=task.lane,
            geography_group=task.query_key, search_mode="DELTA",
        )

    # -- execution ----------------------------------------------------------
    def execute(self, manifest: CoverageManifest, coverage_ids: list[str]) -> PipelineResult:
        result = PipelineResult()
        for coverage_id in coverage_ids:
            task = manifest.get(coverage_id)
            if task is None or task.is_terminal:
                continue
            if task.source_instance not in self.instances:
                # A pseudo-instance (discovery/portal/ats placeholder) has no
                # fixture adapter in this offline run: mark it truthfully as an
                # unresolved extraction rather than pretending it completed.
                manifest.mark(coverage_id, CoverageStatus.EXTRACTION_UNRESOLVED,
                              next_action="NONE", detail={"reason": "no fixture adapter for instance"})
                task_res = TaskExecutionResult(coverage_id, task.lane, CoverageStatus.EXTRACTION_UNRESOLVED, 0, 0)
                result.per_task.append(task_res)
                result.executed += 1
                continue
            task_res = self._execute_one(manifest, task)
            result.per_task.append(task_res)
            result.executed += 1
            result.attempts += task_res.attempts
            result.observations_staged += task_res.observations_staged
            if task_res.sentinel_ran:
                result.sentinels_run += 1
        return result

    def _execute_one(self, manifest: CoverageManifest, task: CoverageTask) -> TaskExecutionResult:
        source_task, sig = self._build_source_task(task)
        adapter = self._adapter_for(task.source_instance)
        worker = SourceSearchWorker(
            {task.source_instance: adapter}, {task.coverage_id: source_task},
            history_provider=self._history_for_signature, executor=self.executor,
        )
        started_at = _utcnow()
        manifest.mark(task.coverage_id, CoverageStatus.IN_PROGRESS, started_at=started_at, next_action="SEARCH")

        attempt_number = 0
        staged = 0
        sentinel_ran = False
        while True:
            attempt_number += 1
            try:
                outcome = worker.attempt(task.coverage_id, attempt_number)
            except WorkerError as exc:
                decision = retry.evaluate(exc.category, attempt_number, self.retry_budget)
                self._record_attempt(
                    task, sig, attempt_number, status=f"FAILED_{exc.category.value}",
                    detail={"error_category": exc.category.value, "retry": decision.should_retry,
                            "reason": decision.reason},
                )
                if decision.should_retry:
                    continue
                terminal = decision.terminal_status or TaskStatus.PERMANENT_FAILURE
                self._record_health(task, sig, attempt_number, _STATUS_HEALTH.get(terminal, "UNKNOWN"), None)
                cov = coverage_status_for(terminal)
                manifest.mark(task.coverage_id, cov, completed_at=_utcnow(),
                              next_action="HUMAN" if cov == CoverageStatus.BLOCKED_HUMAN else "NONE",
                              detail={"terminal_status": terminal.value})
                return TaskExecutionResult(task.coverage_id, task.lane, cov, attempt_number, staged, sentinel_ran)

            # Successful/neutral outcome.
            payload = outcome.payload
            result_count = int(payload.get("result_count", 0))
            zero_kind = payload.get("zero_result_kind")
            sentinel_info = payload.get("sentinel")
            if isinstance(sentinel_info, dict) and sentinel_info.get("ran"):
                sentinel_ran = True
            attempt_id = self._record_attempt(
                task, sig, attempt_number, status=outcome.status.value, jobs_found=result_count,
                detail={"zero_result_kind": zero_kind, "sentinel": sentinel_info,
                        "parse_findings": payload.get("parse_findings", [])},
            )
            staged += self._stage_observations(task, sig, attempt_id, payload)
            self._record_health(
                task, sig, attempt_number, _STATUS_HEALTH.get(outcome.status, "HEALTHY"), result_count
            )
            from atlas.sources.models import ZeroResultKind

            zk = None
            if zero_kind is not None:
                try:
                    zk = ZeroResultKind(zero_kind)
                except ValueError:
                    zk = None
            cov = coverage_status_for(outcome.status, result_count=result_count, zero_kind=zk)
            manifest.mark(
                task.coverage_id, cov, jobs_found=result_count, completed_at=_utcnow(),
                next_action="HUMAN" if cov == CoverageStatus.BLOCKED_HUMAN else "NONE",
                detail={"zero_result_kind": zero_kind},
            )
            return TaskExecutionResult(task.coverage_id, task.lane, cov, attempt_number, staged, sentinel_ran)


__all__ = [
    "FixtureExecutionPipeline",
    "PipelineResult",
    "TaskExecutionResult",
    "canonical_dedupe_key",
]
