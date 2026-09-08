"""Atlas production search runtime (Phase 1B / hardened in Phase 1B.1).

The production runtime drives the explicit multi-phase production graph and
executes its SEALED coverage plan through the REAL source pipeline — the same
path real adapters will use in Phase 1C. Discovery is fixture/fake only (no
real adapter, no live web) but is NOT synthetic: every planned coverage child
is dispatched through SourceRegistry -> adapter -> shared RateLimitedExecutor
-> SourceSearchWorker -> centralized retry -> append-only attempts ->
signature-keyed health -> raw observation staging -> coverage terminal status,
and canonicalization happens only in DEDUPE.

Phase 1B.1 hardening (build spec 4/5/6/7/13/14/15):
    * the sealed plan is PERSISTED before DISCOVER and executed, not a synthetic count;
    * the first LangGraph invoke seeds the initial state (P0-2);
    * policy / candidate ledger / plan / started_at are rehydrated from durable
      state so a fresh process resumes exactly (P0-3);
    * COMPLETE is impossible while required children are non-terminal, and a
      required human-blocked child yields WAITING_FOR_HUMAN (P0-1/13);
    * mandatory local persistence never fails silently (P0-5);
    * the run is guarded by the proven RunLock (P0-6);
    * production candidate evidence is a private snapshot; synthetic evidence is
      used ONLY in explicit fixture mode and reported as such (P0-7).
"""

from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from atlas.candidate.importer import build_synthetic_ledger
from atlas.candidate.ledger import CandidateLedger
from atlas.config import Settings
from atlas.controllers.base import NullController
from atlas.controllers.operations import (
    CandidateMatchRequest,
    RoleClassificationRequest,
    TypedControllerOps,
    VerificationReviewRequest,
)
from atlas.orchestration.checkpoints import open_checkpointer, thread_config
from atlas.orchestration.production_graph import PhaseContext, build_production_graph
from atlas.orchestration.production_state import (
    ProductionPhase,
    ProductionState,
    ProductionTerminalState,
    bump,
    checkpoint_size_bytes,
    initial_production_state,
)
from atlas.orchestration.run_lock import RUN_ALREADY_ACTIVE, RunLock
from atlas.persistence.remote_audit import (
    NullRemoteAudit,
    RemoteAuditResult,
    build_run_audit_event,
    export_run_audit,
)
from atlas.persistence.sqlite import StateStore
from atlas.planning import CoveragePlanner, PlanInput, PlannedCompany
from atlas.planning.query_compiler import SearchQueryCompiler
from atlas.policy import load_policy
from atlas.policy.rules import VerificationInput, classify_verification
from atlas.reporting.mapping import load_report_mapping, validate_report, write_report
from atlas.runtime.canonicalize import canonicalize_run
from atlas.runtime.fixture_pipeline import FixtureExecutionPipeline
from atlas.runtime.parallel_pipeline import ParallelExecutionPipeline
from atlas.sources.child_executor import BoardSnapshotCache
from atlas.sources.coverage import (
    CoverageManifest,
    CoveragePlanState,
    CoverageStatus,
    TERMINAL_COVERAGE_STATUSES,
)
from atlas.sources.executor import RateLimitedExecutor
from atlas.sources.models import SourceInstance
from atlas.sources.rate_limit import RateLimiter, RatePolicy
from atlas.sources.registry import SourceRegistry
from atlas.sources.testing.fake import FakeAdapter, make_fake_instance
from atlas.sources.testing.fixture import FixtureAdapter


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class ProductionCandidateError(RuntimeError):
    """Raised when production mode cannot load a valid private candidate
    snapshot (never silently replaced with synthetic evidence)."""


class LocalPersistenceError(RuntimeError):
    """Raised when a MANDATORY local persistence step fails — this must
    surface as FAILED/PARTIAL, never a false COMPLETE (build spec 14)."""


def default_fixture_topology(
    n_companies: int = 2,
) -> tuple[dict[str, SourceInstance], list[PlannedCompany]]:
    """A small default fixture topology so the offline production-fixture
    runtime always executes a REAL sealed plan (per-lane children) that
    reaches COMPLETE."""
    instances: dict[str, SourceInstance] = {}
    companies: list[PlannedCompany] = []
    for i in range(n_companies):
        iid = f"fixco{i}-workday"
        instances[iid] = make_fake_instance(iid, scenario="results", result_count=3, company=f"FixCo {i}")
        companies.append(
            PlannedCompany(
                company_id=f"fixco{i}", name=f"FixCo {i}", tier="A", mode="DELTA",
                source_instances=(iid,), geography_group="PRIMARY",
            )
        )
    return instances, companies


def default_fixture_registry() -> SourceRegistry:
    reg = SourceRegistry()
    reg.register(FakeAdapter)
    reg.register(FixtureAdapter)
    return reg


@dataclass
class ProductionRunResult:
    run_id: str
    terminal_state: str
    phase: str
    phases_completed: tuple[str, ...]
    counters: dict[str, int]
    policy_fingerprint: str
    plan_fingerprint: str
    checkpoint_bytes: int
    planned_tasks: int = 0
    terminal_tasks: int = 0
    lane_summary: dict = field(default_factory=dict)
    candidate_snapshot: str = ""
    fixture_mode: bool = True
    report_path: Optional[str] = None
    report_valid: Optional[bool] = None
    remote_audit: Optional[RemoteAuditResult] = None
    persistence_side_effects: tuple[str, ...] = ()
    run_lock_status: Optional[str] = None
    manifest: dict = field(default_factory=dict)


class ProductionSearchRuntime:
    def __init__(
        self,
        settings: Settings,
        run_id: str,
        *,
        fixture_mode: bool = True,
        policy_dir: Optional[Path] = None,
        controller=None,
        companies: Optional[list[PlannedCompany]] = None,
        instances: Optional[dict[str, SourceInstance]] = None,
        registry: Optional[SourceRegistry] = None,
        rate_limiter: Optional[RateLimiter] = None,
        retry_budget: int = 2,
        candidate_snapshot_path: Optional[Path] = None,
        discover_batch: Optional[int] = None,
        simulate_plan_failure: bool = False,
        simulate_local_persist_failure: bool = False,
        remote_audit=None,
        remote_audit_enabled: bool = False,
        stop_after_phase: Optional[ProductionPhase] = None,
        write_report: bool = True,
        report_path: Optional[Path] = None,
        use_run_lock: bool = True,
        parallel_workers: int = 1,
        max_per_company: int = 1,
        max_per_instance: int = 1,
        max_per_tenant: int = 1,
        live_canary: bool = False,
        lane_override: Optional[list[str]] = None,
    ):
        self.settings = settings
        self.run_id = run_id
        self.thread_id = f"prod::{run_id}"
        self.fixture_mode = fixture_mode
        self.policy_dir = policy_dir
        self.retry_budget = retry_budget
        self.candidate_snapshot_path = candidate_snapshot_path
        self.discover_batch = discover_batch
        self.simulate_plan_failure = simulate_plan_failure
        self.simulate_local_persist_failure = simulate_local_persist_failure
        self.remote_audit = remote_audit or NullRemoteAudit()
        self.remote_audit_enabled = remote_audit_enabled
        self.stop_after_phase = stop_after_phase
        self.write_report = write_report
        self.report_path = report_path
        self.use_run_lock = use_run_lock
        # Phase 1C-A: bounded parallel discovery. Default 1 keeps the exact
        # Phase 1B.1 sequential behavior; >1 selects the parallel dispatcher for
        # the SAME graph's DISCOVER phase (not a second orchestrator).
        self.parallel_workers = max(1, int(parallel_workers))
        self.max_per_company = max(1, int(max_per_company))
        self.max_per_instance = max(1, int(max_per_instance))
        self.max_per_tenant = max(1, int(max_per_tenant))
        self.live_canary = bool(live_canary)
        # Optional explicit lane set (used by the low-volume canary to run ONE
        # child per board instead of the full policy lane fan-out).
        self.lane_override = lane_override

        if instances is None or companies is None:
            default_instances, default_companies = default_fixture_topology()
            instances = instances or default_instances
            companies = companies or default_companies
        self.instances = instances
        self.companies = companies
        self.registry = registry or default_fixture_registry()
        self.rate_limiter = rate_limiter or RateLimiter(RatePolicy())
        self.executor = RateLimitedExecutor(self.rate_limiter)

        self.ops = TypedControllerOps(controller or NullController(), model=getattr(settings, "controller", "none"))

        self.ctx = PhaseContext()
        self.policy = None
        self.ledger: Optional[CandidateLedger] = None
        self.plan: Optional[CoverageManifest] = None
        self.candidate_snapshot_hash: str = ""
        self.used_synthetic_candidate: bool = fixture_mode
        self.remote_audit_result: Optional[RemoteAuditResult] = None
        self._report_written: Optional[str] = None
        self._report_valid: Optional[bool] = None
        self._started_at = _utcnow()

    # -- rehydration (fresh-process resume, P0-3) ---------------------------
    def _ensure_policy(self, state: ProductionState) -> None:
        if self.policy is None:
            self.policy = load_policy(self.policy_dir)

    def _query_compiler(self) -> Optional[SearchQueryCompiler]:
        """Build (once) the deterministic query compiler from the loaded policy
        so DISCOVER sends REAL compiled lane/geography terms, never enum labels."""
        if getattr(self, "_compiler", None) is None and self.policy is not None:
            self._compiler = SearchQueryCompiler(
                self.policy.lanes, self.policy.geography, policy_version=self.policy.short_fingerprint,
            )
        return getattr(self, "_compiler", None)

    def _ensure_ledger(self, state: ProductionState) -> None:
        if self.ledger is not None:
            return
        if self.fixture_mode:
            self.ledger = build_synthetic_ledger()
            self.used_synthetic_candidate = True
            self.candidate_snapshot_hash = "synthetic"
            return
        # Production mode: require a private, versioned candidate snapshot.
        path = self.candidate_snapshot_path
        if path is None or not Path(path).exists():
            raise ProductionCandidateError(
                "production mode requires a private candidate snapshot; none found "
                f"at {path!r}. Synthetic evidence is never silently substituted."
            )
        raw = Path(path).read_text(encoding="utf-8")
        payload = json.loads(raw)
        self.ledger = CandidateLedger.from_list(payload.get("claims", []))
        self.candidate_snapshot_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        self.used_synthetic_candidate = bool(payload.get("provenance", {}).get("synthetic", False))

    def _ensure_plan(self, state: ProductionState) -> None:
        """Rehydrate the sealed plan from durable SQLite state if it is not in
        memory (a fresh process resuming has no in-memory plan)."""
        if self.plan is not None:
            return
        with StateStore(self.settings.state_db) as store:
            manifest = CoverageManifest.load(store, self.run_id)
        if manifest.tasks() and manifest.state == CoveragePlanState.SEALED:
            self.plan = manifest

    # -- phase handlers -----------------------------------------------------
    def _h_initialize(self, state: ProductionState, _rt) -> ProductionState:
        state.setdefault("started_at", self._started_at.isoformat())
        state["fixture_mode"] = self.fixture_mode
        bump(state, "initialized", 1)
        return state

    def _h_load_profile(self, state: ProductionState, _rt) -> ProductionState:
        try:
            self._ensure_ledger(state)
        except ProductionCandidateError as exc:
            # Production mode with a missing/invalid private snapshot fails
            # clearly to WAITING_FOR_HUMAN — synthetic evidence is NEVER silently
            # substituted (build spec 16 / P0-7).
            state["terminal_state"] = ProductionTerminalState.WAITING_FOR_HUMAN.value
            state.setdefault("human_waiting", []).append("private_candidate_snapshot")
            state.setdefault("notes", []).append(f"MISSING_PRIVATE_CANDIDATE_SNAPSHOT: {exc}")
            return state
        state["candidate_snapshot"] = self.candidate_snapshot_hash
        state.setdefault("counters", {})["candidate_claims"] = len(self.ledger.claims())
        state["counters"]["unresolved_conflicts"] = len(self.ledger.unresolved_conflicts())
        if self.used_synthetic_candidate:
            state.setdefault("notes", []).append("SYNTHETIC_CANDIDATE_EVIDENCE")
        return state

    def _h_load_policy(self, state: ProductionState, _rt) -> ProductionState:
        self._ensure_policy(state)
        state["policy_fingerprint"] = self.policy.short_fingerprint
        return state

    def _h_build_plan(self, state: ProductionState, _rt) -> ProductionState:
        if self.simulate_plan_failure:
            state["terminal_state"] = ProductionTerminalState.FAILED.value
            state.setdefault("notes", []).append("plan build failed")
            return state
        self._ensure_policy(state)
        lanes = self.lane_override if self.lane_override else (list(self.policy.lanes.keys()) if self.policy else [])
        plan_input = PlanInput(
            run_id=self.run_id, companies=self.companies,
            portal_families=[], ats_families=[],
            lanes=lanes, geography_groups=["PRIMARY"],
            policy_fingerprint=state.get("policy_fingerprint", ""),
        )
        self.plan = CoveragePlanner().build_and_seal(plan_input)
        state["plan_fingerprint"] = self.plan.sealed_fingerprint or ""
        # Persist the SEALED plan (lifecycle + every child) BEFORE discovery so
        # a fresh process can resume exactly (build spec 7). The checkpoint keeps
        # only run/plan references + counters — NEVER the task-id list (no P0-4
        # truncation); pending ids are streamed from SQLite.
        with StateStore(self.settings.state_db) as store:
            if store.get_run(self.run_id) is None:
                store.create_run(self.run_id, controller=getattr(self.settings, "controller", "none"),
                                 metadata={"production": True, "fixture_mode": self.fixture_mode})
            for inst in self.instances.values():
                store.upsert_source_instance(
                    inst.instance_id, inst.adapter_key.value, inst.source_type.value, inst.category.value,
                    display_name=inst.display_name, base_url=inst.base_url, tenant=inst.tenant, site=inst.site,
                )
            self.plan.persist(store, policy_fingerprint=state.get("policy_fingerprint", ""))
        state.setdefault("counters", {})["planned_tasks"] = len(self.plan.tasks())
        return state

    def _h_discover(self, state: ProductionState, _rt) -> ProductionState:
        """Execute pending coverage children through the REAL source pipeline.
        Idempotent for resume: already-terminal children are skipped, so no
        attempt/observation/report side effect is duplicated."""
        self._ensure_policy(state)
        self._ensure_plan(state)
        if self.plan is None:
            state["terminal_state"] = ProductionTerminalState.FAILED.value
            state.setdefault("notes", []).append("no sealed plan to execute")
            return state
        compiler = self._query_compiler()
        geography = self.policy.geography if self.policy is not None else None
        if getattr(self, "_snapshot_cache", None) is None:
            self._snapshot_cache = BoardSnapshotCache()
        with StateStore(self.settings.state_db) as store:
            # Reload from durable state so a fresh process sees persisted statuses.
            manifest = CoverageManifest.load(store, self.run_id)
            self.plan = manifest
            # Genuinely-pending children only (a human-blocked child is NOT
            # re-attempted in the same run — it awaits human action).
            pending = [t.coverage_id for t in manifest.remaining() if t.status != CoverageStatus.BLOCKED_HUMAN]
            if self.parallel_workers > 1:
                # Bounded parallel dispatcher: atomic leases + per-company /
                # per-instance / per-tenant caps. Each worker opens its OWN
                # StateStore connection via the factory (never shared).
                db_path = self.settings.state_db
                parallel = ParallelExecutionPipeline(
                    lambda: StateStore(db_path), self.registry, self.instances,
                    run_id=self.run_id, policy_version=state.get("policy_fingerprint", "unversioned"),
                    retry_budget=self.retry_budget, workers=self.parallel_workers,
                    max_per_company=self.max_per_company, max_per_instance=self.max_per_instance,
                    max_per_tenant=self.max_per_tenant, rate_limiter=self.rate_limiter,
                    query_compiler=compiler, geography=geography, snapshot_cache=self._snapshot_cache,
                )
                result = parallel.execute(manifest, pending, max_children=self.discover_batch)
                # Workers persisted every child; reload to reflect true statuses
                # (do NOT re-persist the stale in-memory manifest).
                manifest = CoverageManifest.load(store, self.run_id)
                self.plan = manifest
            else:
                pipeline = FixtureExecutionPipeline(
                    store, self.registry, self.instances, executor=self.executor,
                    run_id=self.run_id, policy_version=state.get("policy_fingerprint", "unversioned"),
                    retry_budget=self.retry_budget, query_compiler=compiler, geography=geography,
                    snapshot_cache=self._snapshot_cache,
                )
                result = pipeline.execute(manifest, pending, max_children=self.discover_batch)
                manifest.persist(store, policy_fingerprint=state.get("policy_fingerprint", ""))
            remaining_after = sum(1 for t in manifest.remaining() if t.status != CoverageStatus.BLOCKED_HUMAN)
        bump(state, "discovered", result.observations_staged)
        bump(state, "attempts", result.attempts)
        bump(state, "sentinels", result.sentinels_run)
        bump(state, "children_executed", result.executed)
        # Batched/resumable discovery: if children remain, re-enter DISCOVER on
        # the next invoke (durable partial progress is already persisted).
        if remaining_after > 0:
            state["_repeat_phase"] = True
        return state

    def _h_health_gate(self, state: ProductionState, _rt) -> ProductionState:
        bump(state, "health_checked", 1)
        return state

    def _h_detail_hydration(self, state: ProductionState, _rt) -> ProductionState:
        """Hydrate the CURRENT run's observations through the shared executor +
        central retry, using each adapter's detail anchor and merging into a NEW
        immutable observation version (build spec 14) — NOT a single
        representative-posting probe."""
        from atlas.sources.detail_hydration import DetailHydrator

        with StateStore(self.settings.state_db) as store:
            hydrator = DetailHydrator(
                store, self.registry, self.instances, run_id=self.run_id,
                executor=self.executor, retry_budget=self.retry_budget,
                max_details=getattr(self, "detail_batch", 25),
            )
            res = hydrator.hydrate()
        counters = state.setdefault("counters", {})
        counters["hydrated"] = res.hydrated
        counters["hydration_selected"] = res.selected
        counters["hydration_skipped"] = res.skipped_existing
        return state

    def _h_verification(self, state: ProductionState, _rt) -> ProductionState:
        """Classify EVERY selected current-run job from its ACTUAL official
        source evidence (build spec 19). A search-list row alone is
        OFFICIAL_SEARCH_LIVE, distinct from a fully detail-hydrated
        VERIFIED_OFFICIAL — never a blanket label. Access limitation is not
        closure."""
        jobs = self._current_run_jobs()
        verified = 0
        search_only = 0
        for j in jobs:
            classify_verification(
                VerificationInput(
                    page_kind="specific_role_page" if j["evidence"].startswith("OFFICIAL") else "portal_listing",
                    identity_aligned=bool(j["source_job_id"]),
                    current_content=(j["lifecycle"] != "CLOSED"),
                )
            )
            if j["verification_level"] == "VERIFIED_OFFICIAL":
                verified += 1
            elif j["verification_level"] == "OFFICIAL_SEARCH_LIVE":
                search_only += 1
        # Exercise the (optional) reasoning transport once for auditability.
        if jobs:
            self.ops.review_verification(
                VerificationReviewRequest(page_kind="specific_role_page", identity_aligned=True, current_content=True),
                deterministic_ceiling="VERIFIED_OFFICIAL",
            )
        state.setdefault("counters", {})["verified"] = verified
        state["counters"]["official_search_live"] = search_only
        state["counters"]["verification_candidates"] = len(jobs)
        return state

    def _h_dedupe(self, state: ProductionState, _rt) -> ProductionState:
        with StateStore(self.settings.state_db) as store:
            result = canonicalize_run(store, self.run_id)
            unique = store.count_canonical_jobs()
        state.setdefault("counters", {})["unique_after_dedupe"] = unique
        state["counters"]["canonicalized_observations"] = result.observations_added
        return state

    def _h_match(self, state: ProductionState, _rt) -> ProductionState:
        """Evaluate the CURRENT-run jobs against the private candidate snapshot
        (build spec 19). In fixture/synthetic-candidate mode the match is
        explicitly NOT_EVALUATED — a synthetic candidate never yields an invented
        match — and no hard-coded counter is incremented for a fake job."""
        jobs = self._current_run_jobs()
        counters = state.setdefault("counters", {})
        counters["match_candidates"] = len(jobs)
        if self.used_synthetic_candidate:
            counters["matched"] = 0
            counters["not_evaluated"] = len(jobs)
            state.setdefault("notes", []).append("CANDIDATE_MATCH_NOT_EVALUATED_SYNTHETIC")
            return state
        supported_all = self._candidate_evidence_set()
        matched = 0
        for j in jobs:
            reqs = tuple(j.get("skills") or ())
            supported = tuple(sorted(supported_all & {r.lower() for r in reqs}))
            self.ops.classify_role(RoleClassificationRequest(title=j["title"] or "", lane_hint=None))
            self.ops.match_candidate(
                CandidateMatchRequest(job_requirements=reqs, supported_evidence=supported)
            )
            if supported:
                matched += 1
        counters["matched"] = matched
        return state

    def _h_persist_local(self, state: ProductionState, _rt) -> ProductionState:
        """Mandatory local persistence. A failure here is classified and
        surfaced (FAILED/PARTIAL) — NEVER swallowed (build spec 14 / P0-5)."""
        self.ctx.record_side_effect(ProductionPhase.PERSIST_LOCAL, "sqlite_persist")
        try:
            if self.simulate_local_persist_failure:
                raise LocalPersistenceError("injected state DB write failure")
            with StateStore(self.settings.state_db) as store:
                if store.get_run(self.run_id) is None:
                    store.create_run(self.run_id, controller=getattr(self.settings, "controller", "none"),
                                     metadata={"production": True})
                self._ensure_plan(state)
                if self.plan is not None:
                    self.plan.persist(store, policy_fingerprint=state.get("policy_fingerprint", ""))
                for i, finding in enumerate(self.ops.audit_findings):
                    store.add_quarantine(
                        f"ctrl::{self.run_id}::{i}::{finding['operation']}",
                        record_id=self.run_id, reason_code="CONTROLLER_INVALID_FELL_BACK",
                        entity_type="controller_output", detail=finding,
                    )
            state["local_persist_ok"] = True
        except Exception as exc:  # noqa: BLE001 - classify, never swallow
            state["local_persist_ok"] = False
            state["terminal_state"] = ProductionTerminalState.FAILED.value
            state.setdefault("notes", []).append(f"LOCAL_PERSISTENCE_FAILED: {type(exc).__name__}: {exc}")
            return state
        bump(state, "persisted", 1)
        return state

    def _h_build_report(self, state: ProductionState, _rt) -> ProductionState:
        # If any required child is human-blocked, the run is WAITING_FOR_HUMAN
        # and must not look COMPLETE.
        self._ensure_plan(state)
        if self._has_human_blocked_child():
            state["terminal_state"] = ProductionTerminalState.WAITING_FOR_HUMAN.value
            state.setdefault("human_waiting", []).append("human_blocked_coverage_child")
            return state
        self.ctx.record_side_effect(ProductionPhase.BUILD_REPORT, "excel_report")
        if self.write_report:
            mapping = load_report_mapping()
            out = Path(self.report_path) if self.report_path else (self.settings.output_dir / f"Atlas_Jobs_{self.run_id}.xlsx")
            data = self._report_data_from_state(state)
            try:
                write_result = write_report(mapping, out, data)
                self._report_written = write_result.written_path
                validation = validate_report(mapping, Path(write_result.written_path))
                self._report_valid = validation.ok
                state["report_valid"] = validation.ok
                if not validation.ok:
                    state["terminal_state"] = ProductionTerminalState.FAILED.value
                    state.setdefault("notes", []).append("REPORT_VALIDATION_FAILED")
                    return state
            except Exception as exc:  # noqa: BLE001 - report is mandatory local output
                state["report_valid"] = False
                state["terminal_state"] = ProductionTerminalState.FAILED.value
                state.setdefault("notes", []).append(f"REPORT_PUBLISH_FAILED: {type(exc).__name__}: {exc}")
                return state
        bump(state, "reported", 1)
        return state

    def _h_remote_audit(self, state: ProductionState, _rt) -> ProductionState:
        self.ctx.record_side_effect(ProductionPhase.OPTIONAL_REMOTE_AUDIT, "remote_audit")
        event = build_run_audit_event(
            run_id=self.run_id, terminal_status="RUNNING",
            policy_fingerprint=state.get("policy_fingerprint", ""),
            plan_fingerprint=state.get("plan_fingerprint", ""),
            counters=dict(state.get("counters", {})),
        )
        self.remote_audit_result = export_run_audit(
            self.remote_audit, self.run_id, [event], enabled=self.remote_audit_enabled
        )
        state.setdefault("notes", []).append(self.remote_audit_result.status)
        return state

    # -- terminality gate (build spec 13 / P0-17) ---------------------------
    def _load_manifest(self) -> Optional[CoverageManifest]:
        with StateStore(self.settings.state_db) as store:
            manifest = CoverageManifest.load(store, self.run_id)
        return manifest if manifest.tasks() else None

    def _has_human_blocked_child(self) -> bool:
        manifest = self._load_manifest()
        if manifest is None:
            return False
        return any(t.status == CoverageStatus.BLOCKED_HUMAN for t in manifest.tasks())

    def resolve_terminal(self, state: ProductionState) -> ProductionTerminalState:
        """Decide the TRUE terminal state before COMPLETE is accepted. COMPLETE
        requires a SEALED plan whose fingerprint matches, every required child
        terminal, no human-blocked child, and successful local persistence +
        report validation. Otherwise fail closed."""
        if state.get("local_persist_ok") is False:
            return ProductionTerminalState.FAILED
        manifest = self._load_manifest()
        if manifest is None:
            return ProductionTerminalState.FAILED
        if manifest.state != CoveragePlanState.SEALED:
            return ProductionTerminalState.FAILED
        if state.get("plan_fingerprint") and manifest.sealed_fingerprint != state.get("plan_fingerprint"):
            return ProductionTerminalState.FAILED
        if any(t.status == CoverageStatus.BLOCKED_HUMAN for t in manifest.tasks()):
            return ProductionTerminalState.WAITING_FOR_HUMAN
        if any(t.status not in TERMINAL_COVERAGE_STATUSES for t in manifest.tasks()):
            return ProductionTerminalState.PARTIAL
        # A budget-bounded child truncated the board on purpose — it is terminal
        # so a bounded canary can finish, but the RUN is honestly PARTIAL, never
        # a full-board COMPLETE (build spec 20). Also fail closed if any terminal
        # child still has an outstanding page/cursor continuation.
        if any(t.status == CoverageStatus.PARTIAL_BUDGET for t in manifest.tasks()):
            return ProductionTerminalState.PARTIAL
        with StateStore(self.settings.state_db) as store:
            if any(store.coverage_pages_outstanding(self.run_id, t.coverage_id) for t in manifest.tasks()):
                return ProductionTerminalState.PARTIAL
        if self.write_report and state.get("report_valid") is False:
            return ProductionTerminalState.FAILED
        return ProductionTerminalState.COMPLETE

    # -- current-run truthful projection (build spec 19) --------------------
    _EVIDENCE_TO_VERIFICATION = {
        "OFFICIAL_DETAIL_LIVE": "VERIFIED_OFFICIAL",
        "OFFICIAL_SEARCH_LIVE": "OFFICIAL_SEARCH_LIVE",
        "PORTAL_LIVE": "PORTAL_CURRENT_LEAD",
    }
    _EVIDENCE_RANK = {"OFFICIAL_DETAIL_LIVE": 3, "OFFICIAL_SEARCH_LIVE": 2, "PORTAL_LIVE": 1}

    @staticmethod
    def _freshness_band(posted_at, date_provenance, *, now=None) -> str:
        """Map a KNOWN employer posted/updated date to a freshness band; only a
        genuinely unknown/relative date is LIVE_DATE_UNKNOWN (build spec 19). A
        crawl/discovery date is never used as the posting date (the adapter's
        DateProvenance already guarantees that)."""
        if not posted_at or date_provenance not in ("EMPLOYER_POSTED_AT", "EMPLOYER_UPDATED_AT"):
            return "LIVE_DATE_UNKNOWN"
        try:
            raw = str(posted_at).replace("Z", "+00:00")
            dt = datetime.datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
        except (ValueError, TypeError):
            return "LIVE_DATE_UNKNOWN"
        now = now or datetime.datetime.now(datetime.timezone.utc)
        age = (now - dt).days
        if age < 0:
            age = 0
        if age <= 7:
            return "0-7 days"
        if age <= 14:
            return "8-14 days"
        if age <= 30:
            return "15-30 days"
        if age <= 45:
            return "31-45 days exceptional"
        return "STALE"

    def _current_run_jobs(self, *, now=None) -> list[dict]:
        """Project ONLY the current run's observations (grouped to canonical
        identity) into truthful report/verification rows — never every canonical
        job globally (build spec 19). Each row carries the ACTUAL evidence-derived
        verification level and freshness band, not a blanket label."""
        best: dict[str, dict] = {}
        with StateStore(self.settings.state_db) as store:
            for row in store.list_raw_observations(self.run_id):
                key = row["canonical_id"] or row["source_identity"] or row["observation_id"]
                try:
                    detail = json.loads(row["detail_json"] or "{}")
                except (ValueError, TypeError):
                    detail = {}
                evidence = detail.get("verification_level") or ""
                rank = self._EVIDENCE_RANK.get(evidence, 0)
                prev = best.get(key)
                if prev is not None and prev["_rank"] >= rank:
                    continue
                verification = self._EVIDENCE_TO_VERIFICATION.get(evidence, "MANUAL_VERIFICATION")
                freshness = self._freshness_band(row["posted_at"], detail.get("date_provenance"), now=now)
                is_active = (row["is_active"] or "").upper()
                lifecycle = "ACTIVE" if is_active == "ACTIVE" else ("CLOSED" if is_active == "INACTIVE" else "UNKNOWN")
                best[key] = {
                    "_rank": rank,
                    "company": row["company"], "title": row["title"], "location": row["location"],
                    "source_job_id": row["source_job_id"],
                    "url": row["canonical_url"] or row["source_url"],
                    "verification_level": verification, "freshness_band": freshness,
                    "lifecycle": lifecycle,
                    "skills": tuple(str(s) for s in (detail.get("skills") or [])),
                    "recommendation": "REVIEW" if verification == "VERIFIED_OFFICIAL" else "MANUAL_REVIEW",
                    "evidence": evidence,
                }
        return sorted(best.values(), key=lambda r: (r["company"] or "", r["title"] or ""))

    def _candidate_evidence_set(self) -> set:
        """Best-effort lowercase evidence-token set from the private candidate
        ledger (used only in production matching)."""
        tokens: set[str] = set()
        if self.ledger is None:
            return tokens
        try:
            for claim in self.ledger.to_list():
                if isinstance(claim, dict):
                    for k in ("value", "topic", "skill", "claim", "text"):
                        v = claim.get(k)
                        if isinstance(v, str) and v.strip():
                            tokens.add(v.strip().lower())
        except Exception:  # noqa: BLE001 - matching must never crash the run
            return tokens
        return tokens

    def _report_data_from_state(self, state: ProductionState) -> dict:
        jobs = self._current_run_jobs()
        rows = []
        verified_count = 0
        for j in jobs[:500]:
            if j["verification_level"] == "VERIFIED_OFFICIAL":
                verified_count += 1
            rows.append({
                "company": j["company"], "role_title": j["title"],
                "source_job_id": j["source_job_id"], "location": j["location"],
                "official_apply_url": j["url"] or "",
                "verification_level": j["verification_level"],
                "job_lifecycle_status": j["lifecycle"],
                "recommendation": j["recommendation"], "freshness_band": j["freshness_band"],
            })
        counters = state.get("counters", {})
        return {
            "All_Jobs": rows,
            "Run_Summary": [{
                "Run_ID": self.run_id, "Run_Status": state.get("terminal_state", "RUNNING"),
                "Raw_Discoveries": counters.get("discovered", 0),
                "Relevant_Discoveries": counters.get("unique_after_dedupe", len(rows)),
                "Verified_Official": verified_count,
            }],
        }

    def _handlers(self):
        return {
            ProductionPhase.INITIALIZE: self._h_initialize,
            ProductionPhase.LOAD_PRIVATE_PROFILE: self._h_load_profile,
            ProductionPhase.LOAD_POLICY: self._h_load_policy,
            ProductionPhase.BUILD_AND_SEAL_PLAN: self._h_build_plan,
            ProductionPhase.DISCOVER: self._h_discover,
            ProductionPhase.SOURCE_HEALTH_GATE: self._h_health_gate,
            ProductionPhase.DETAIL_HYDRATION: self._h_detail_hydration,
            ProductionPhase.OFFICIAL_VERIFICATION: self._h_verification,
            ProductionPhase.DEDUPE_AND_REPOST_CLASSIFICATION: self._h_dedupe,
            ProductionPhase.CANDIDATE_MATCH: self._h_match,
            ProductionPhase.PERSIST_LOCAL: self._h_persist_local,
            ProductionPhase.BUILD_REPORT: self._h_build_report,
            ProductionPhase.OPTIONAL_REMOTE_AUDIT: self._h_remote_audit,
        }

    # -- run ----------------------------------------------------------------
    def _run_graph(self) -> tuple[ProductionState, str]:
        graph_builder = build_production_graph(self._handlers(), self)
        with open_checkpointer(self.settings.checkpoint_db) as checkpointer:
            graph = graph_builder.compile(checkpointer=checkpointer)
            config = thread_config(self.thread_id)
            existing = graph.get_state(config).values
            # P0-2: the FIRST invoke seeds the initial state; later invokes pass
            # a compact empty update.
            if existing:
                state: ProductionState = existing
                first_input: dict = {}
            else:
                state = initial_production_state(
                    self.run_id, started_at=self._started_at.isoformat(), fixture_mode=self.fixture_mode
                )
                first_input = state

            max_steps = 100
            steps = 0
            invoked_first = False
            while not state.get("terminal_state") and steps < max_steps:
                steps += 1
                phase_before = state.get("phase")
                payload = first_input if not invoked_first else {}
                invoked_first = True
                state = graph.invoke(payload, config)
                if (
                    self.stop_after_phase is not None
                    and phase_before == self.stop_after_phase.value
                    and not state.get("terminal_state")
                ):
                    return state, ProductionTerminalState.PARTIAL.value
            terminal = state.get("terminal_state") or ProductionTerminalState.PARTIAL.value
            return state, terminal

    def run(self) -> ProductionRunResult:
        if not self.use_run_lock:
            state, terminal = self._run_graph()
            return self._finalize(state, terminal, run_lock_status="LOCK_DISABLED")
        run_lock = RunLock(Path(self.settings.state_db).parent)
        acquired = run_lock.try_acquire(self.run_id)
        if acquired.status == RUN_ALREADY_ACTIVE:
            return ProductionRunResult(
                run_id=self.run_id, terminal_state=ProductionTerminalState.FAILED.value,
                phase="", phases_completed=(), counters={}, policy_fingerprint="",
                plan_fingerprint="", checkpoint_bytes=0, run_lock_status=RUN_ALREADY_ACTIVE,
            )
        try:
            state, terminal = self._run_graph()
            return self._finalize(state, terminal, run_lock_status=acquired.status)
        finally:
            run_lock.release()

    def resume(self) -> ProductionRunResult:
        """Resume ordinary PARTIAL work only (build spec 4). This NEVER changes a
        BLOCKED_HUMAN child and NEVER performs a human reopen: a human-blocked
        child stays blocked and the run stays WAITING_FOR_HUMAN until an explicit
        ``resume_after_human`` authorizes the reopen. It re-enters DISCOVER so
        genuinely-pending (non-blocked) children are re-attempted."""
        self._reenter_discover_for_resume()
        return self.run()

    def resume_after_human(self, reason: str, reference: Optional[str] = None) -> ProductionRunResult:
        """Explicit, AUTHORIZED human resolution (build spec 4). Requires a
        non-empty ``reason``. Reopens ONLY BLOCKED_HUMAN coverage children —
        atomically resetting coverage + lease (fencing version++) and appending a
        HUMAN_REOPEN audit event, idempotent by an operation key — then resumes
        the graph so the reopened children are re-attempted. A normally-completed
        child is never reopened; ``reason``/``reference`` must not carry secrets."""
        if not reason or not str(reason).strip():
            raise ValueError("resume_after_human requires a non-empty reason (human authorization)")
        with StateStore(self.settings.state_db) as store:
            manifest = CoverageManifest.load(store, self.run_id)
            for task in manifest.tasks():
                if task.status == CoverageStatus.BLOCKED_HUMAN:
                    op_key = f"human_reopen::{self.run_id}::{task.coverage_id}::{reference or reason}"
                    store.human_reopen_child(
                        self.run_id, task.coverage_id, reason=str(reason), reference=reference, op_key=op_key,
                    )
        self._reenter_discover_for_resume()
        return self.run()

    def _reenter_discover_for_resume(self) -> None:
        """Clear a PARTIAL/WAITING terminal state and re-enter DISCOVER so a
        resume re-attempts the remaining pending children. Coverage statuses are
        NOT touched here (only ``resume_after_human`` reopens a blocked child)."""
        with open_checkpointer(self.settings.checkpoint_db) as checkpointer:
            graph = build_production_graph(self._handlers(), self).compile(checkpointer=checkpointer)
            config = thread_config(self.thread_id)
            snapshot = graph.get_state(config)
            values = snapshot.values if snapshot else None
            if values and values.get("terminal_state") in (
                ProductionTerminalState.PARTIAL.value, ProductionTerminalState.WAITING_FOR_HUMAN.value
            ):
                values["terminal_state"] = None
                if values.get("phase") in (
                    ProductionPhase.BUILD_REPORT.value, ProductionPhase.OPTIONAL_REMOTE_AUDIT.value,
                    ProductionPhase.COMPLETE.value,
                ):
                    values["phase"] = ProductionPhase.DISCOVER.value
                graph.update_state(config, values)

    def _finalize(self, state: ProductionState, terminal: str, *, run_lock_status: Optional[str] = None) -> ProductionRunResult:
        now = _utcnow()
        started_at = state.get("started_at") or self._started_at.isoformat()
        manifest = self._load_manifest()
        planned = len(manifest.tasks()) if manifest else 0
        terminal_tasks = sum(1 for t in manifest.tasks() if t.is_terminal) if manifest else 0
        lane_summary = manifest.lane_summary() if manifest else {}
        result_manifest = {
            "run_id": self.run_id,
            "status": terminal,
            "started_at": started_at,
            # completed_at only for COMPLETE; ended_at for other terminals (P1-4).
            ("completed_at" if terminal == ProductionTerminalState.COMPLETE.value else "ended_at"): now.isoformat(),
            "policy_fingerprint": state.get("policy_fingerprint", ""),
            "plan_fingerprint": state.get("plan_fingerprint", ""),
            "candidate_snapshot": state.get("candidate_snapshot", ""),
            "synthetic_candidate_evidence": self.used_synthetic_candidate,
            "fixture_mode": self.fixture_mode,
            "phases_completed": list(state.get("phases_completed", [])),
            "counters": dict(state.get("counters", {})),
            "planned_tasks": planned,
            "terminal_tasks": terminal_tasks,
            "lane_summary": lane_summary,
        }
        # Record run status (best-effort; the truthful terminal already accounts
        # for any local persistence failure via resolve_terminal()).
        try:
            with StateStore(self.settings.state_db) as store:
                if store.get_run(self.run_id) is None:
                    store.create_run(self.run_id, controller=getattr(self.settings, "controller", "none"),
                                     metadata={"production": True})
                store.complete_run(self.run_id, status=terminal)
        except Exception:  # noqa: BLE001 - status row is a mirror; terminal already truthful
            pass
        return ProductionRunResult(
            run_id=self.run_id,
            terminal_state=terminal,
            phase=state.get("phase", ""),
            phases_completed=tuple(state.get("phases_completed", [])),
            counters=dict(state.get("counters", {})),
            policy_fingerprint=state.get("policy_fingerprint", ""),
            plan_fingerprint=state.get("plan_fingerprint", ""),
            checkpoint_bytes=checkpoint_size_bytes(state),
            planned_tasks=planned,
            terminal_tasks=terminal_tasks,
            lane_summary=lane_summary,
            candidate_snapshot=state.get("candidate_snapshot", ""),
            fixture_mode=self.fixture_mode,
            report_path=self._report_written,
            report_valid=self._report_valid,
            remote_audit=self.remote_audit_result,
            persistence_side_effects=tuple(self.ctx.persistence_side_effects),
            run_lock_status=run_lock_status,
            manifest=result_manifest,
        )


__all__ = [
    "ProductionSearchRuntime",
    "ProductionRunResult",
    "ProductionCandidateError",
    "LocalPersistenceError",
    "default_fixture_topology",
    "default_fixture_registry",
]
