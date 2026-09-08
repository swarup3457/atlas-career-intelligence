"""Shared per-coverage-child execution (Phase 1C-A, build spec 8).

Extracts the ONE per-child execution path — search → centralized retry →
append-only attempt → signature-keyed health → raw observation staging →
terminal coverage status — into a single, manifest-free unit of work used by
BOTH the sequential :class:`atlas.runtime.fixture_pipeline.FixtureExecutionPipeline`
AND the bounded parallel dispatcher. Because both call the exact same code for a
child, a run at concurrency 1 and a run at concurrency N produce byte-identical
canonical results; only scheduling differs.

A ``CoverageChildExecutor`` is bound to ONE :class:`StateStore` connection, so
each parallel worker constructs its own (never sharing a sqlite connection
across threads). It persists attempts/health/observations through that store but
does NOT mutate any in-memory manifest and does NOT write the coverage row — the
caller owns coverage persistence (an in-memory ``manifest.mark`` for the
sequential path, or ``store.upsert_coverage`` for a parallel worker), so the two
callers stay behaviorally identical while differing only in how the terminal
status is recorded.
"""

from __future__ import annotations

import datetime
import hashlib
from dataclasses import dataclass, field
from typing import Mapping, Optional

from atlas.models import TaskStatus
from atlas.orchestration import retry
from atlas.sources.coverage import CoverageStatus, CoverageTask, coverage_status_for
from atlas.sources.executor import RateLimitedExecutor
from atlas.sources.models import SearchRequest, SourceInstance, ZeroResultKind
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


@dataclass
class ChildExecutionOutcome:
    """The terminal result of executing one coverage child. Manifest-free: the
    caller applies ``status`` (and the timestamps/counters) to whatever
    coverage record it owns."""

    coverage_id: str
    lane: Optional[str]
    status: CoverageStatus
    attempts: int
    observations_staged: int
    sentinel_ran: bool
    jobs_found: int
    started_at: str
    completed_at: str
    next_action: str
    detail: dict = field(default_factory=dict)


class CrashInjection(RuntimeError):
    """Raised by an injected fault hook to simulate a worker crash mid-child
    (e.g. between the HTTP response and status persistence) for recovery tests.
    It escapes ``execute`` so the caller/lease layer can prove reclaim + no
    duplicate side effects."""


class CoverageChildExecutor:
    """Executes exactly one coverage child through the real source pipeline."""

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
        crash_hook=None,
    ):
        self.store = store
        self.registry = registry
        self.instances = dict(instances)
        self.executor = executor or RateLimitedExecutor()
        self.run_id = run_id
        self.policy_version = policy_version
        self.retry_budget = retry_budget
        self.limit = limit
        self.crash_hook = crash_hook
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
    def execute(self, task: CoverageTask) -> ChildExecutionOutcome:
        source_task, sig = self._build_source_task(task)
        adapter = self._adapter_for(task.source_instance)
        worker = SourceSearchWorker(
            {task.source_instance: adapter}, {task.coverage_id: source_task},
            history_provider=self._history_for_signature, executor=self.executor,
        )
        started_at = _utcnow()

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
                return ChildExecutionOutcome(
                    task.coverage_id, task.lane, cov, attempt_number, staged, sentinel_ran,
                    jobs_found=0, started_at=started_at, completed_at=_utcnow(),
                    next_action="HUMAN" if cov == CoverageStatus.BLOCKED_HUMAN else "NONE",
                    detail={"terminal_status": terminal.value},
                )

            # Successful/neutral outcome. Optional fault injection AFTER the
            # adapter call/response but BEFORE we persist a terminal status, to
            # prove a crash here leaves a reclaimable lease and no duplicate.
            if self.crash_hook is not None:
                self.crash_hook(task, attempt_number)

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
            zk = None
            if zero_kind is not None:
                try:
                    zk = ZeroResultKind(zero_kind)
                except ValueError:
                    zk = None
            cov = coverage_status_for(outcome.status, result_count=result_count, zero_kind=zk)
            return ChildExecutionOutcome(
                task.coverage_id, task.lane, cov, attempt_number, staged, sentinel_ran,
                jobs_found=result_count, started_at=started_at, completed_at=_utcnow(),
                next_action="HUMAN" if cov == CoverageStatus.BLOCKED_HUMAN else "NONE",
                detail={"zero_result_kind": zero_kind},
            )


__all__ = [
    "CoverageChildExecutor",
    "ChildExecutionOutcome",
    "CrashInjection",
    "canonical_dedupe_key",
]
