"""Bounded parallel coverage dispatcher (Phase 1C-A, build spec 6 & 8).

The parallel analogue of the sequential
:class:`atlas.runtime.fixture_pipeline.FixtureExecutionPipeline`. It executes the
SAME sealed coverage plan through the SAME per-child code
(:class:`atlas.sources.child_executor.CoverageChildExecutor`), so concurrency 1
and concurrency N produce identical canonical results — only scheduling differs.
This is NOT a second orchestrator: it is the discover-phase execution strategy
the ONE LangGraph production graph selects when configured for parallelism.

Design:

    * ONE deterministic dispatcher (main thread) leases the exact coverage child
      atomically, respects per-company / per-source-instance / per-tenant
      concurrency caps, and submits bounded work to a ``ThreadPoolExecutor``.
    * MANY bounded workers each open their OWN StateStore connection (never
      sharing a sqlite connection across threads), execute one leased child,
      persist its coverage row / attempts / observations, and mark the lease
      terminal. A worker that crashes releases its lease for reclaim.
    * The central retry policy still decides retries (inside the shared child
      executor); the dispatcher never re-implements retry.
    * Excel/report writing NEVER happens in a worker — only fan-in accounting.

Liveness: whenever no work is in flight, all caps are zero, so at least one
remaining child is always eligible — the dispatcher cannot deadlock on caps.
"""

from __future__ import annotations

import itertools
import threading
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Callable, Mapping, Optional

from atlas.runtime.fixture_pipeline import PipelineResult, TaskExecutionResult
from atlas.sources.child_executor import ChildExecutionOutcome, CoverageChildExecutor, CrashInjection
from atlas.sources.coverage import CoverageManifest, CoverageStatus, CoverageTask
from atlas.sources.executor import RateLimitedExecutor
from atlas.sources.leasing import (
    ClockFn,
    Lease,
    LeaseHeartbeat,
    LeaseManager,
    LeaseMutationResult,
    system_utc_clock,
)
from atlas.sources.models import SourceInstance
from atlas.sources.rate_limit import RateLimiter, RatePolicy
from atlas.sources.registry import SourceRegistry

StoreFactory = Callable[[], object]


@dataclass
class _WorkerResult:
    coverage_id: str
    crashed: bool
    outcome: Optional[ChildExecutionOutcome]


class ParallelExecutionPipeline:
    """Executes pending coverage children through a bounded parallel worker
    pool with atomic task leasing. API-compatible with
    :meth:`FixtureExecutionPipeline.execute`."""

    def __init__(
        self,
        store_factory: StoreFactory,
        registry: SourceRegistry,
        instances: Mapping[str, SourceInstance],
        *,
        run_id: str,
        policy_version: str = "unversioned",
        retry_budget: int = 2,
        limit: int = 25,
        workers: int = 4,
        max_per_company: int = 1,
        max_per_instance: int = 1,
        max_per_tenant: int = 1,
        report_writers: int = 1,  # documented invariant: reports are never written by a worker
        ttl_seconds: float = 120.0,
        lease_clock: ClockFn = system_utc_clock,
        rate_limiter: Optional[RateLimiter] = None,
        max_reclaims: int = 3,
        child_crash_hook: Optional[Callable[[CoverageTask, int], None]] = None,
        enable_heartbeat: bool = True,
        heartbeat_interval_seconds: Optional[float] = None,
        query_compiler=None,
        geography=None,
        snapshot_cache=None,
        max_pages: int = 50,
    ):
        if workers < 1:
            raise ValueError("workers must be >= 1")
        if report_writers != 1:
            # Reports are ALWAYS written by the single governor, never by a
            # worker; more (or zero) report writers is a contradiction.
            raise ValueError("report_writers must be exactly 1 (the governor writes the one report)")
        for name, cap in (("max_per_company", max_per_company), ("max_per_instance", max_per_instance),
                          ("max_per_tenant", max_per_tenant)):
            if cap < 1:
                raise ValueError(f"{name} must be >= 1")
        self.store_factory = store_factory
        self.registry = registry
        self.instances = dict(instances)
        self.run_id = run_id
        self.policy_version = policy_version
        self.retry_budget = retry_budget
        self.limit = limit
        self.workers = workers
        self.max_per_company = max_per_company
        self.max_per_instance = max_per_instance
        self.max_per_tenant = max_per_tenant
        self.report_writers = report_writers
        self.ttl_seconds = ttl_seconds
        self.lease_clock = lease_clock
        # One SHARED rate limiter/executor across workers so per-instance pacing
        # and the (now-fixed) concurrency slot are enforced globally, not per
        # thread. It is thread-safe (internal lock/condition).
        self._executor = RateLimitedExecutor(rate_limiter or RateLimiter(RatePolicy()))
        self.max_reclaims = max_reclaims
        self.child_crash_hook = child_crash_hook
        self.enable_heartbeat = enable_heartbeat
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        # Query compilation + a SHARED, thread-safe board-snapshot cache so a
        # list-only board is fetched once and reused across every lane worker.
        self.query_compiler = query_compiler
        self.geography = geography
        self.snapshot_cache = snapshot_cache
        self.max_pages = max_pages
        self._worker_seq = itertools.count(1)
        self._cap_lock = threading.Lock()

    # -- helpers ------------------------------------------------------------
    def _tenant_of(self, task: CoverageTask) -> str:
        inst = self.instances.get(task.source_instance)
        if inst is not None and inst.tenant:
            return f"tenant::{inst.tenant}"
        return f"company::{task.company or task.source_instance}"

    def _new_worker_id(self) -> str:
        return f"w{next(self._worker_seq)}"

    def _select_eligible(
        self,
        remaining: set[str],
        task_by_id: Mapping[str, CoverageTask],
        active_company: Mapping[str, int],
        active_instance: Mapping[str, int],
        active_tenant: Mapping[str, int],
    ) -> Optional[str]:
        for cid in sorted(remaining):
            task = task_by_id[cid]
            company = task.company or task.source_instance
            tenant = self._tenant_of(task)
            if active_company.get(company, 0) >= self.max_per_company:
                continue
            if active_instance.get(task.source_instance, 0) >= self.max_per_instance:
                continue
            if active_tenant.get(tenant, 0) >= self.max_per_tenant:
                continue
            return cid
        return None

    # -- execution ----------------------------------------------------------
    def execute(
        self, manifest: CoverageManifest, coverage_ids: list[str], *, max_children: Optional[int] = None
    ) -> PipelineResult:
        result = PipelineResult()
        dispatch_store = self.store_factory()
        lease_mgr = LeaseManager(dispatch_store, self.run_id, ttl_seconds=self.ttl_seconds, clock=self.lease_clock)

        task_by_id: dict[str, CoverageTask] = {}
        for cid in coverage_ids:
            task = manifest.get(cid)
            if task is None:
                continue
            terminal = task.is_terminal or task.status == CoverageStatus.BLOCKED_HUMAN
            # Children with no configured adapter are not leasable here — mark
            # them EXTRACTION_UNRESOLVED directly (mirrors sequential path).
            if not terminal and task.source_instance not in self.instances:
                dispatch_store.upsert_coverage(
                    task.coverage_id, self.run_id, task.source_instance, company=task.company,
                    source_type=task.source_type, lane=task.lane, query_key=task.query_key,
                    attempted=True, completed=True, status=CoverageStatus.EXTRACTION_UNRESOLVED.value,
                    next_action="NONE", detail={"reason": "no adapter for instance"},
                )
                lease_mgr.ensure(cid, company=task.company, source_instance=task.source_instance,
                                 tenant=self._tenant_of(task), terminal=True)
                result.per_task.append(
                    TaskExecutionResult(cid, task.lane, CoverageStatus.EXTRACTION_UNRESOLVED, 0, 0)
                )
                result.executed += 1
                continue
            lease_mgr.ensure(cid, company=task.company, source_instance=task.source_instance,
                             tenant=self._tenant_of(task), terminal=terminal)
            if not terminal:
                task_by_id[cid] = task

        remaining: set[str] = set(task_by_id)
        active_company: dict[str, int] = defaultdict(int)
        active_instance: dict[str, int] = defaultdict(int)
        active_tenant: dict[str, int] = defaultdict(int)
        reclaim_counts: dict[str, int] = defaultdict(int)
        futures: dict = {}
        executed_ids: set[str] = set()

        pool = ThreadPoolExecutor(max_workers=self.workers)
        try:
            while remaining or futures:
                # Submit as much bounded work as caps + pool + batch budget allow.
                while len(futures) < self.workers and (
                    max_children is None or (len(executed_ids) + len(futures)) < max_children
                ):
                    with self._cap_lock:
                        cid = self._select_eligible(remaining, task_by_id, active_company, active_instance, active_tenant)
                    if cid is None:
                        break
                    remaining.discard(cid)
                    worker_id = self._new_worker_id()
                    lease = lease_mgr.acquire(cid, worker_id)
                    if lease is None:
                        # Already terminal / held elsewhere — do not re-queue.
                        continue
                    task = task_by_id[cid]
                    company = task.company or task.source_instance
                    tenant = self._tenant_of(task)
                    with self._cap_lock:
                        active_company[company] += 1
                        active_instance[task.source_instance] += 1
                        active_tenant[tenant] += 1
                    fut = pool.submit(self._run_child, task, lease)
                    futures[fut] = (cid, company, tenant, task.source_instance)

                if not futures:
                    break

                done, _pending = wait(list(futures), return_when=FIRST_COMPLETED)
                for fut in done:
                    cid, company, tenant, instance_id = futures.pop(fut)
                    with self._cap_lock:
                        active_company[company] -= 1
                        active_instance[instance_id] -= 1
                        active_tenant[tenant] -= 1
                    try:
                        wr: _WorkerResult = fut.result()
                    except Exception:  # noqa: BLE001 - an unexpected worker crash
                        wr = _WorkerResult(cid, crashed=True, outcome=None)
                    if wr.crashed or wr.outcome is None:
                        reclaim_counts[cid] += 1
                        if reclaim_counts[cid] <= self.max_reclaims:
                            remaining.add(cid)  # lease was released → reclaimable
                        else:
                            self._give_up(dispatch_store, lease_mgr, task_by_id[cid])
                            result.per_task.append(
                                TaskExecutionResult(cid, task_by_id[cid].lane, CoverageStatus.FAILED, 0, 0)
                            )
                            result.executed += 1
                        continue
                    executed_ids.add(cid)
                    outcome = wr.outcome
                    result.executed += 1
                    result.attempts += outcome.attempts
                    result.observations_staged += outcome.observations_staged
                    if outcome.sentinel_ran:
                        result.sentinels_run += 1
                    result.per_task.append(
                        TaskExecutionResult(cid, outcome.lane, outcome.status, outcome.attempts,
                                            outcome.observations_staged, outcome.sentinel_ran)
                    )
        finally:
            pool.shutdown(wait=True)
            dispatch_store.close()
        return result

    def _give_up(self, store, lease_mgr: LeaseManager, task: CoverageTask) -> None:
        store.upsert_coverage(
            task.coverage_id, self.run_id, task.source_instance, company=task.company,
            source_type=task.source_type, lane=task.lane, query_key=task.query_key,
            attempted=True, completed=True, status=CoverageStatus.FAILED.value,
            next_action="NONE", detail={"reason": "exceeded max reclaims after repeated worker loss"},
        )
        lease_mgr.complete(task.coverage_id, terminal_status="FAILED")

    def _run_child(self, task: CoverageTask, lease: Lease) -> _WorkerResult:
        """Runs on a pool thread with its OWN StateStore connection. The lease
        token fences every mutation; a periodic heartbeat keeps a long child's
        lease unreclaimable; ANY unexpected exception releases the owned lease in
        ``finally`` so it is reclaimable and NOTHING partial is persisted."""
        worker_id = lease.worker_id
        store = self.store_factory()
        heartbeat: Optional[LeaseHeartbeat] = None
        try:
            lease_mgr = LeaseManager(store, self.run_id, ttl_seconds=self.ttl_seconds, clock=self.lease_clock)
            child = CoverageChildExecutor(
                store, self.registry, self.instances, executor=self._executor, run_id=self.run_id,
                policy_version=self.policy_version, retry_budget=self.retry_budget, limit=self.limit,
                crash_hook=self.child_crash_hook, query_compiler=self.query_compiler,
                geography=self.geography, snapshot_cache=self.snapshot_cache, max_pages=self.max_pages,
            )
            if self.enable_heartbeat:
                heartbeat = LeaseHeartbeat(
                    self.store_factory, self.run_id, lease, ttl_seconds=self.ttl_seconds,
                    clock=self.lease_clock, interval_seconds=self.heartbeat_interval_seconds,
                )
                heartbeat.start()
            try:
                outcome = child.execute(task)
            except CrashInjection:
                # Simulated crash between HTTP response and status persistence:
                # release the (fenced) lease so it is reclaimable; persist NOTHING.
                if heartbeat is not None:
                    heartbeat.stop(); heartbeat = None
                lease_mgr.release(lease, requeue=True)
                return _WorkerResult(task.coverage_id, crashed=True, outcome=None)
            except Exception:  # noqa: BLE001 - any unexpected worker fault
                # Build spec 8: an unexpected ValueError/AdapterError/persistence
                # exception must never strand an owned lease. Release it (fenced)
                # so it is reclaimable; persist nothing partial.
                if heartbeat is not None:
                    heartbeat.stop(); heartbeat = None
                try:
                    lease_mgr.release(lease, requeue=True)
                except Exception:  # noqa: BLE001
                    pass
                return _WorkerResult(task.coverage_id, crashed=True, outcome=None)
            finally:
                if heartbeat is not None:
                    heartbeat.stop(); heartbeat = None
            store.upsert_coverage(
                task.coverage_id, self.run_id, task.source_instance, company=task.company,
                source_type=task.source_type, lane=task.lane, query_key=task.query_key,
                attempted=True, completed=outcome.status in _TERMINAL_SET,
                status=outcome.status.value, jobs_found=outcome.jobs_found,
                started_at=outcome.started_at, completed_at=outcome.completed_at,
                next_action=outcome.next_action, detail=outcome.detail,
            )
            # Mirror coverage terminality onto the lease (FENCED) so it is never
            # re-leased in this run (a human-blocked child likewise awaits human
            # action). A stale-token rejection here means another worker already
            # reclaimed+finished it — harmless, our terminal write was superseded.
            lease_mgr.complete(lease, terminal_status=outcome.status.value)
            return _WorkerResult(task.coverage_id, crashed=False, outcome=outcome)
        finally:
            if heartbeat is not None:
                heartbeat.stop()
            store.close()


from atlas.sources.coverage import TERMINAL_COVERAGE_STATUSES as _TERMINAL_COVERAGE_STATUSES  # noqa: E402

_TERMINAL_SET = set(_TERMINAL_COVERAGE_STATUSES) | {CoverageStatus.BLOCKED_HUMAN}


__all__ = ["ParallelExecutionPipeline"]
