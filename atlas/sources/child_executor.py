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
import json
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Mapping, Optional

from atlas.models import TaskStatus
from atlas.orchestration import retry
from atlas.sources.coverage import CoverageStatus, CoverageTask, coverage_status_for
from atlas.sources.executor import RateLimitedExecutor
from atlas.sources.models import Capability, SearchRequest, SourceInstance, SourceType, ZeroResultKind
from atlas.sources.query_signature import QuerySignature
from atlas.sources.registry import SourceRegistry
from atlas.sources.untrusted import redact_secrets
from atlas.sources.worker import SourceSearchWorker, SourceTask
from atlas.workers.base import WorkerError


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# The maximum number of characters of any description/large text field we allow
# into the bounded observation detail — never an unbounded HTML page.
_MAX_DETAIL_TEXT = 2000


class BoardSnapshotCache:
    """A thread-safe, run-scoped memoization of a WHOLE-board fetch per source
    instance (build spec 11). A list-only board (Greenhouse/Ashby) is fetched
    ONCE and the same normalized snapshot then feeds every lane/geography child —
    so six lanes never trigger six identical network acquisitions. ``fetch_count``
    records how many real network acquisitions happened per instance so a test
    can prove fan-out reuse."""

    def __init__(self):
        self._global_lock = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._snaps: dict[str, dict] = {}
        self.fetch_count: dict[str, int] = defaultdict(int)

    def get_or_fetch(self, key: str, fetch_fn) -> dict:
        with self._global_lock:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            if key in self._snaps:
                return self._snaps[key]
            snap = fetch_fn()
            self.fetch_count[key] += 1
            self._snaps[key] = snap
            return snap



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
        query_compiler=None,
        geography=None,
        snapshot_cache: Optional[BoardSnapshotCache] = None,
        max_pages: int = 50,
        apply_relevance: Optional[bool] = None,
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
        # Deterministic query compilation (build spec 11): compile real lane/
        # geography terms instead of sending the enum label as a search string.
        self.query_compiler = query_compiler
        self.geography = geography
        self.snapshot_cache = snapshot_cache
        self.max_pages = max(1, int(max_pages))
        # Local lane relevance filtering: None = AUTO (on for real boards when a
        # compiler is present, but never for synthetic FAKE/FIXTURE doubles),
        # True/False force it on/off explicitly.
        self._relevance_param = apply_relevance
        self.apply_relevance = (query_compiler is not None) if apply_relevance is None else apply_relevance
        self._adapters: dict[str, object] = {}

    # -- helpers ------------------------------------------------------------
    def _adapter_for(self, instance_id: str):
        if instance_id not in self._adapters:
            self._adapters[instance_id] = self.registry.create(self.instances[instance_id])
        return self._adapters[instance_id]

    def _history_for_signature(self, fingerprint: str):
        return self.store.recent_yields_for_signature(fingerprint)

    def _compiled_for(self, task: CoverageTask):
        """Compile the real query for this lane × geography, or ``None`` when no
        compiler is configured (legacy fake-adapter path)."""
        if self.query_compiler is None or not task.lane:
            return None
        geo = task.query_key or "PRIMARY"
        try:
            return self.query_compiler.compile(task.lane, geo, mode="DELTA")
        except Exception:  # noqa: BLE001 - a bad lane key must not crash the child
            return None

    def _list_only(self, instance_id: str) -> bool:
        """A whole-board (list-only) family declares SEARCH but not PAGINATION."""
        adapter = self._adapter_for(instance_id)
        caps = getattr(adapter, "CAPABILITIES", frozenset())
        return Capability.SEARCH in caps and Capability.PAGINATION not in caps

    def _signature(self, task: CoverageTask, compiled, *, cursor_family: Optional[str] = None) -> QuerySignature:
        instance = self.instances[task.source_instance]
        geo = task.query_key or "PRIMARY"
        keywords = compiled.signature_keywords() if compiled is not None else ([task.lane] if task.lane else None)
        filters = {"geo_terms": list(compiled.geography_terms)} if compiled is not None else None
        return QuerySignature.for_instance(
            instance, lane=task.lane, geography_group=geo, search_mode="DELTA",
            keywords=keywords, filters=filters, cursor_family=cursor_family,
            policy_version=self.policy_version,
        )

    def _build_source_task(
        self, task: CoverageTask, compiled, *, page: int = 1, cursor: Optional[str] = None, limit: Optional[int] = None,
    ) -> tuple[SourceTask, QuerySignature]:
        """Build the per-page search task. Uses the COMPILED query terms and
        geography (never the lane/group enum label as a literal search string)."""
        geo = task.query_key or "PRIMARY"
        sig = self._signature(task, compiled, cursor_family=("page" if page > 1 or cursor else None))
        if compiled is not None:
            query = compiled.primary_query or (task.lane or "software")
            location = compiled.geography_terms[0] if compiled.geography_terms else None
        else:
            query = task.lane or "software"
            location = geo
        page_limit = limit if limit is not None else self.limit
        request = SearchRequest(
            query=query, location=location, company=task.company, limit=page_limit, page=page, cursor=cursor,
        )
        sentinel = SearchRequest(
            query=query, location=location, company=task.company, limit=page_limit,
            extra_filters={"sentinel": True},
        )
        return (
            SourceTask(
                item_id=task.coverage_id, instance_id=task.source_instance, request=request,
                lane=task.lane, company=task.company, sentinel_request=sentinel, query_signature=sig,
            ),
            sig,
        )

    def _relevant(self, task: CoverageTask, compiled, raw: dict) -> bool:
        """Local lane + geography relevance for a whole-board snapshot child."""
        if compiled is None or not self.apply_relevance:
            return True
        inst = self.instances.get(task.source_instance)
        # AUTO mode never lane-filters a synthetic FAKE/FIXTURE double (its
        # titles are not real job titles); real boards and an explicit
        # apply_relevance=True always filter.
        if self._relevance_param is None and inst is not None and inst.source_type in (SourceType.FAKE, SourceType.FIXTURE):
            return True
        if not compiled.is_relevant(raw.get("title"), description=raw.get("description")):
            return False
        # Geography filtering only when a policy geography is available AND the
        # posting carries a location; a missing location does not exclude.
        if self.geography is not None and raw.get("location"):
            if not compiled.location_in_group(raw.get("location"), self.geography):
                return False
        return True

    def _observation_detail(self, raw: dict) -> dict:
        """Build the BOUNDED, secret-redacted provenance detail persisted with a
        staged observation (build spec 12). Every typed field is preserved; a
        description/large-text field is truncated to a bounded, redacted fragment
        so no unbounded HTML — and no credentials/cookies/Authorization — enters
        the store."""
        def _bounded(text):
            if not text:
                return text
            red = redact_secrets(str(text))
            return red[:_MAX_DETAIL_TEXT] + (" …[truncated]" if len(red) > _MAX_DETAIL_TEXT else "")

        prov = dict(raw.get("provenance") or {})
        detail = {
            "work_mode": raw.get("work_mode"),
            "updated_at": raw.get("updated_at"),
            "deadline": raw.get("deadline"),
            "employment_type": raw.get("employment_type"),
            "experience_text": _bounded(raw.get("experience_text")),
            "salary_text": raw.get("salary_text"),
            "skills": list(raw.get("skills") or []),
            "description_fragment": _bounded(raw.get("description")),
            "description_len": len(raw.get("description") or "") if raw.get("description") else 0,
            "verification_level": raw.get("verification_level"),
            "is_active": raw.get("is_active"),
            "confidence": raw.get("confidence"),
            "adapter_version": raw.get("adapter_version"),
            "parser_version": raw.get("parser_version"),
            "date_provenance": prov.get("date_provenance"),
            "provenance": prov,
            "detail_anchor": raw.get("source_job_id") or raw.get("canonical_url") or raw.get("source_url"),
        }
        return detail

    def _stage_observations(self, task: CoverageTask, sig: QuerySignature, attempt_id: str,
                            payload: dict, compiled=None) -> int:
        """Stage each relevant posting with FULL bounded provenance (build spec
        12). A lane-relevance filter (when a compiler is present) keeps each lane
        independent; every typed field is preserved in a bounded, secret-redacted
        detail (no unbounded HTML, no credentials)."""
        staged = 0
        for i, raw in enumerate(payload.get("results", []) or []):
            if not self._relevant(task, compiled, raw):
                continue
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
                detail=self._observation_detail(raw),
            )
            if inserted:
                staged += 1
        return staged

    def _record_attempt(self, task: CoverageTask, sig: QuerySignature, attempt_label,
                        status: str, *, jobs_found: int = 0, detail: Optional[dict] = None) -> str:
        attempt_id = f"att::{self.run_id}::{task.coverage_id}::{attempt_label}"
        self.store.append_coverage_attempt(
            attempt_id, task.coverage_id, self.run_id, task.source_instance, status,
            query_signature=sig.fingerprint(), jobs_found=jobs_found, detail=detail or {},
        )
        return attempt_id

    def _record_health(self, task: CoverageTask, sig: QuerySignature, attempt_label,
                       state: str, result_count: Optional[int]) -> None:
        self.store.record_source_health(
            f"health::{self.run_id}::{task.coverage_id}::{attempt_label}",
            task.source_instance, state, result_count=result_count,
            query_signature=sig.fingerprint(), lane=task.lane,
            geography_group=task.query_key, search_mode="DELTA",
        )

    # -- one page (with per-page retry) -------------------------------------
    def _run_page(self, task: CoverageTask, compiled, page_index: int, cursor: Optional[str]) -> dict:
        """Execute ONE page (following the centralized retry policy). Returns a
        dict describing either a terminal error or a completed page."""
        source_task, sig = self._build_source_task(task, compiled, page=page_index, cursor=cursor)
        adapter = self._adapter_for(task.source_instance)
        worker = SourceSearchWorker(
            {task.source_instance: adapter}, {task.coverage_id: source_task},
            history_provider=self._history_for_signature, executor=self.executor,
        )
        attempt_number = 0
        while True:
            attempt_number += 1
            label = f"{page_index}.{attempt_number}"
            try:
                outcome = worker.attempt(task.coverage_id, attempt_number)
            except WorkerError as exc:
                decision = retry.evaluate(exc.category, attempt_number, self.retry_budget)
                self._record_attempt(
                    task, sig, label, status=f"FAILED_{exc.category.value}",
                    detail={"error_category": exc.category.value, "retry": decision.should_retry,
                            "reason": decision.reason, "page": page_index},
                )
                if decision.should_retry:
                    continue
                terminal = decision.terminal_status or TaskStatus.PERMANENT_FAILURE
                self._record_health(task, sig, label, _STATUS_HEALTH.get(terminal, "UNKNOWN"), None)
                return {"kind": "error", "cov": coverage_status_for(terminal),
                        "terminal_status": terminal, "attempts": attempt_number, "sig": sig}
            if self.crash_hook is not None:
                self.crash_hook(task, attempt_number)
            payload = outcome.payload
            result_count = int(payload.get("result_count", 0))
            zero_kind = payload.get("zero_result_kind")
            sentinel_info = payload.get("sentinel")
            sentinel_ran = bool(isinstance(sentinel_info, dict) and sentinel_info.get("ran"))
            attempt_id = self._record_attempt(
                task, sig, label, status=outcome.status.value, jobs_found=result_count,
                detail={"zero_result_kind": zero_kind, "sentinel": sentinel_info,
                        "parse_findings": payload.get("parse_findings", []), "page": page_index},
            )
            staged = self._stage_observations(task, sig, attempt_id, payload, compiled)
            self._record_health(task, sig, label, _STATUS_HEALTH.get(outcome.status, "HEALTHY"), result_count)
            zk = None
            if zero_kind is not None:
                try:
                    zk = ZeroResultKind(zero_kind)
                except ValueError:
                    zk = None
            cov = coverage_status_for(outcome.status, result_count=result_count, zero_kind=zk)
            return {"kind": "ok", "cov": cov, "status": outcome.status, "result_count": result_count,
                    "staged": staged, "has_more": bool(payload.get("has_more")),
                    "next_cursor": payload.get("next_cursor"), "total_reported": payload.get("total_reported"),
                    "sentinel_ran": sentinel_ran, "attempt_id": attempt_id, "attempts": attempt_number, "sig": sig}

    # -- shared whole-board snapshot (list-only families) -------------------
    def _resume_point(self, pages: dict) -> tuple[int, Optional[str]]:
        """Return the (page_index, cursor) to (re)start from: the first non-DONE
        page (its stored cursor), else page 1 with no cursor for a fresh child."""
        if not pages:
            return 1, None
        for idx in sorted(pages):
            row = pages[idx]
            if row["status"] != "DONE":
                return idx, row["cursor"]
        return max(pages) + 1, None

    def _acquire_board(self, task: CoverageTask, compiled) -> dict:
        """Perform the SINGLE whole-board network acquisition for a list-only
        family. Runs one worker attempt (lane-agnostic, large limit) through the
        shared executor so it is paced/counted, and returns the raw payload +
        status for reuse across every lane/geography child of this instance."""
        # A neutral, lane-agnostic request: the list endpoint returns the whole
        # board regardless of query, so all lanes share ONE acquisition.
        source_task, sig = self._build_source_task(task, None, page=1, cursor=None, limit=10_000)
        adapter = self._adapter_for(task.source_instance)
        worker = SourceSearchWorker(
            {task.source_instance: adapter}, {task.coverage_id: source_task},
            history_provider=self._history_for_signature, executor=self.executor,
        )
        attempt_number = 0
        while True:
            attempt_number += 1
            try:
                outcome = worker.attempt(task.coverage_id, attempt_number)
            except WorkerError as exc:
                decision = retry.evaluate(exc.category, attempt_number, self.retry_budget)
                if decision.should_retry:
                    continue
                terminal = decision.terminal_status or TaskStatus.PERMANENT_FAILURE
                return {"error": True, "terminal_status": terminal, "cov": coverage_status_for(terminal),
                        "payload": {"results": []}}
            payload = outcome.payload
            zk = None
            if payload.get("zero_result_kind") is not None:
                try:
                    zk = ZeroResultKind(payload["zero_result_kind"])
                except ValueError:
                    zk = None
            cov = coverage_status_for(outcome.status, result_count=int(payload.get("result_count", 0)), zero_kind=zk)
            sentinel_info = payload.get("sentinel")
            return {"error": False, "payload": payload, "status": outcome.status, "cov": cov,
                    "sentinel_ran": bool(isinstance(sentinel_info, dict) and sentinel_info.get("ran"))}

    def _execute_list_only(self, task: CoverageTask, compiled, started_at: str) -> ChildExecutionOutcome:
        """Evaluate ONE lane/geography child from the shared board snapshot,
        performing NO extra network acquisition when the snapshot already
        exists for this instance (build spec 11)."""
        key = f"{self.run_id}::{task.source_instance}"
        snap = self.snapshot_cache.get_or_fetch(key, lambda: self._acquire_board(task, compiled))
        # Record this lane child's attempt + health + page against the shared snapshot.
        sig = self._signature(task, compiled)
        if snap.get("error"):
            self._record_attempt(task, sig, "snap", status=snap["cov"].value, jobs_found=0,
                                 detail={"snapshot_reused": True, "board_error": True})
            self.store.upsert_coverage_page(self.run_id, task.coverage_id, 1, status="DONE", has_more=False)
            return ChildExecutionOutcome(task.coverage_id, task.lane, snap["cov"], 1, 0, False, jobs_found=0,
                                         started_at=started_at, completed_at=_utcnow(), next_action="NONE",
                                         detail={"snapshot_reused": True})
        payload = snap["payload"]
        attempt_id = self._record_attempt(
            task, sig, "snap", status=snap["status"].value, jobs_found=int(payload.get("result_count", 0)),
            detail={"snapshot_reused": True, "board_count": int(payload.get("result_count", 0)),
                    "shared_snapshot_key": key},
        )
        staged = self._stage_observations(task, sig, attempt_id, payload, compiled)
        self._record_health(task, sig, "snap", _STATUS_HEALTH.get(snap["status"], "HEALTHY"),
                            int(payload.get("result_count", 0)))
        self.store.upsert_coverage_page(self.run_id, task.coverage_id, 1, status="DONE", has_more=False,
                                        results_count=staged, total_reported=payload.get("total_reported"),
                                        attempt_id=attempt_id)
        cov = CoverageStatus.COMPLETED_WITH_RESULTS if staged > 0 else CoverageStatus.ATTEMPTED_ZERO
        return ChildExecutionOutcome(task.coverage_id, task.lane, cov, 1, staged, snap.get("sentinel_ran", False),
                                     jobs_found=staged, started_at=started_at, completed_at=_utcnow(),
                                     next_action="NONE",
                                     detail={"snapshot_reused": True, "board_count": int(payload.get("result_count", 0))})

    # -- execution ----------------------------------------------------------
    def execute(self, task: CoverageTask) -> ChildExecutionOutcome:
        compiled = self._compiled_for(task)
        started_at = _utcnow()
        # List-only boards (Greenhouse/Ashby): one shared snapshot feeds all lanes.
        if (self.snapshot_cache is not None and task.source_instance in self.instances
                and self._list_only(task.source_instance)):
            existing = {p["page_index"]: dict(p) for p in self.store.list_coverage_pages(self.run_id, task.coverage_id)}
            if not (existing and all(p["status"] == "DONE" for p in existing.values())):
                return self._execute_list_only(task, compiled, started_at)
        pages = {p["page_index"]: dict(p) for p in self.store.list_coverage_pages(self.run_id, task.coverage_id)}
        seen_cursors = {p["cursor"] for p in pages.values() if p["cursor"]}
        total_staged = sum(int(p["results_count"]) for p in pages.values() if p["status"] == "DONE")
        first_total: Optional[int] = None
        total_shift = False
        attempts_total = 0
        sentinel_ran = False
        last_cov: Optional[CoverageStatus] = None
        page_index, cursor = self._resume_point(pages)
        # A prior fully-DONE child with no outstanding continuation is terminal.
        if pages and all(p["status"] == "DONE" for p in pages.values()) and not self.store.coverage_pages_outstanding(self.run_id, task.coverage_id):
            highest = pages[max(pages)]
            cov = CoverageStatus.COMPLETED_WITH_RESULTS if total_staged > 0 else CoverageStatus.ATTEMPTED_ZERO
            return ChildExecutionOutcome(task.coverage_id, task.lane, cov, 0, total_staged, False,
                                         jobs_found=total_staged, started_at=started_at, completed_at=_utcnow(),
                                         next_action="NONE", detail={"resumed_terminal": True, "pages": len(pages)})

        while True:
            page = self._run_page(task, compiled, page_index, cursor)
            attempts_total += page["attempts"]
            if page["kind"] == "error":
                self.store.upsert_coverage_page(
                    self.run_id, task.coverage_id, page_index, status="FAILED", cursor=cursor, has_more=False,
                )
                cov = page["cov"]
                return ChildExecutionOutcome(
                    task.coverage_id, task.lane, cov, attempts_total, total_staged, sentinel_ran,
                    jobs_found=total_staged, started_at=started_at, completed_at=_utcnow(),
                    next_action="HUMAN" if cov == CoverageStatus.BLOCKED_HUMAN else "NONE",
                    detail={"terminal_status": page["terminal_status"].value, "pages": page_index},
                )
            total_staged += page["staged"]
            last_cov = page["cov"]
            if page["sentinel_ran"]:
                sentinel_ran = True
            rep_total = page["total_reported"]
            if rep_total is not None:
                if first_total is None:
                    first_total = rep_total
                elif rep_total != first_total:
                    total_shift = True
            has_more = page["has_more"]
            next_cursor = page["next_cursor"]
            loop = bool(has_more and next_cursor is not None and next_cursor in seen_cursors)
            budget_hit = bool(has_more and page_index >= self.max_pages)
            # Persist this page's durable state. has_more stays TRUE when we stop
            # early (loop/budget) so the truth that more exists is recorded.
            self.store.upsert_coverage_page(
                self.run_id, task.coverage_id, page_index, status="DONE", cursor=cursor,
                has_more=has_more, results_count=page["staged"], total_reported=rep_total,
                attempt_id=page["attempt_id"],
            )
            if loop:
                return ChildExecutionOutcome(
                    task.coverage_id, task.lane, CoverageStatus.EXTRACTION_UNRESOLVED, attempts_total,
                    total_staged, sentinel_ran, jobs_found=total_staged, started_at=started_at,
                    completed_at=_utcnow(), next_action="NONE",
                    detail={"reason": "cursor_loop", "cursor": next_cursor, "pages": page_index,
                            "total_reported": first_total, "total_shift": total_shift},
                )
            if has_more and not budget_hit:
                # Follow a cursor when the adapter emits one, else advance by page
                # number (page-based pagination). Either way a required next page
                # is durably recorded BEFORE advancing so a crash cannot lose it.
                if next_cursor is not None:
                    seen_cursors.add(next_cursor)
                self.store.upsert_coverage_page(
                    self.run_id, task.coverage_id, page_index + 1, status="PENDING",
                    cursor=next_cursor, has_more=False,
                )
                page_index += 1
                cursor = next_cursor
                continue
            # Terminal.
            if budget_hit:
                cov = CoverageStatus.PARTIAL_BUDGET
                detail = {"budget_exceeded": True, "max_pages": self.max_pages, "pages": page_index,
                          "total_reported": first_total, "total_shift": total_shift,
                          "next_cursor_available": next_cursor}
            elif total_staged > 0:
                cov = CoverageStatus.COMPLETED_WITH_RESULTS
                detail = {"pages": page_index, "total_reported": first_total, "total_shift": total_shift}
            else:
                cov = last_cov or CoverageStatus.ATTEMPTED_ZERO
                detail = {"pages": page_index, "total_reported": first_total, "total_shift": total_shift,
                          "zero_result_kind": None}
            return ChildExecutionOutcome(
                task.coverage_id, task.lane, cov, attempts_total, total_staged, sentinel_ran,
                jobs_found=total_staged, started_at=started_at, completed_at=_utcnow(),
                next_action="HUMAN" if cov == CoverageStatus.BLOCKED_HUMAN else "NONE", detail=detail,
            )


__all__ = [
    "CoverageChildExecutor",
    "ChildExecutionOutcome",
    "CrashInjection",
    "BoardSnapshotCache",
    "canonical_dedupe_key",
]
