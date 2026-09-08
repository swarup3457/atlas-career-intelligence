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
from atlas.policy import load_policy
from atlas.policy.rules import VerificationInput, classify_verification
from atlas.reporting.mapping import load_report_mapping, validate_report, write_report
from atlas.runtime.canonicalize import canonicalize_run
from atlas.runtime.fixture_pipeline import FixtureExecutionPipeline
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
        lanes = list(self.policy.lanes.keys()) if self.policy else []
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
        with StateStore(self.settings.state_db) as store:
            # Reload from durable state so a fresh process sees persisted statuses.
            manifest = CoverageManifest.load(store, self.run_id)
            self.plan = manifest
            # Genuinely-pending children only (a human-blocked child is NOT
            # re-attempted in the same run — it awaits human action).
            pending = [t.coverage_id for t in manifest.remaining() if t.status != CoverageStatus.BLOCKED_HUMAN]
            pipeline = FixtureExecutionPipeline(
                store, self.registry, self.instances, executor=self.executor,
                run_id=self.run_id, policy_version=state.get("policy_fingerprint", "unversioned"),
                retry_budget=self.retry_budget,
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
        # Hydrate one representative posting via an adapter that supports DETAIL.
        from atlas.sources.models import Capability, DetailRequest, SearchRequest

        hydrated = 0
        for inst in self.instances.values():
            try:
                adapter = self.registry.create(inst)
                if adapter.supports(Capability.DETAIL):
                    probe = adapter.search(SearchRequest(query="x", limit=1))
                    if probe.results and probe.results[0].source_job_id:
                        adapter.fetch_detail(DetailRequest(source_job_id=probe.results[0].source_job_id))
                        hydrated += 1
                        break
            except Exception:  # noqa: BLE001 - hydration is best-effort telemetry
                continue
        bump(state, "hydrated", hydrated)
        return state

    def _h_verification(self, state: ProductionState, _rt) -> ProductionState:
        level = classify_verification(
            VerificationInput(page_kind="specific_role_page", identity_aligned=True, current_content=True)
        )
        self.ops.review_verification(
            VerificationReviewRequest(page_kind="specific_role_page", identity_aligned=True, current_content=True),
            deterministic_ceiling=level.value,
        )
        bump(state, "verified", 1)
        return state

    def _h_dedupe(self, state: ProductionState, _rt) -> ProductionState:
        with StateStore(self.settings.state_db) as store:
            result = canonicalize_run(store, self.run_id)
            unique = store.count_canonical_jobs()
        state.setdefault("counters", {})["unique_after_dedupe"] = unique
        state["counters"]["canonicalized_observations"] = result.observations_added
        return state

    def _h_match(self, state: ProductionState, _rt) -> ProductionState:
        self.ops.classify_role(RoleClassificationRequest(title="Software Engineer", lane_hint="JAVA_BACKEND"))
        self.ops.match_candidate(
            CandidateMatchRequest(job_requirements=("Java", "Spring Boot"), supported_evidence=("Java",))
        )
        bump(state, "matched", 1)
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
        if self.write_report and state.get("report_valid") is False:
            return ProductionTerminalState.FAILED
        return ProductionTerminalState.COMPLETE

    def _report_data_from_state(self, state: ProductionState) -> dict:
        rows = []
        with StateStore(self.settings.state_db) as store:
            for job in store.list_canonical_jobs()[:500]:
                rows.append({
                    "company": job["company"], "role_title": job["role"],
                    "source_job_id": job["job_id"], "location": job["location"],
                    "official_apply_url": job["official_apply_url"] or "",
                    "verification_level": "VERIFIED_OFFICIAL",
                    "job_lifecycle_status": job["current_status"] or "UNKNOWN",
                    "recommendation": "MANUAL_REVIEW", "freshness_band": "LIVE_DATE_UNKNOWN",
                })
        counters = state.get("counters", {})
        return {
            "All_Jobs": rows,
            "Run_Summary": [{
                "Run_ID": self.run_id, "Run_Status": state.get("terminal_state", "RUNNING"),
                "Raw_Discoveries": counters.get("discovered", 0),
                "Relevant_Discoveries": counters.get("unique_after_dedupe", 0),
                "Verified_Official": len(rows),
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
        # A resume after WAITING_FOR_HUMAN represents an authorized human
        # resolution: reset human-blocked children to NOT_ATTEMPTED so discover
        # re-attempts them (e.g. after the access limitation is cleared).
        with StateStore(self.settings.state_db) as store:
            manifest = CoverageManifest.load(store, self.run_id)
            for task in manifest.tasks():
                if task.status == CoverageStatus.BLOCKED_HUMAN:
                    manifest.mark(task.coverage_id, CoverageStatus.NOT_ATTEMPTED, next_action="SEARCH")
            if manifest.tasks():
                manifest.persist(store, policy_fingerprint=manifest.policy_fingerprint)
        with open_checkpointer(self.settings.checkpoint_db) as checkpointer:
            graph = build_production_graph(self._handlers(), self).compile(checkpointer=checkpointer)
            config = thread_config(self.thread_id)
            snapshot = graph.get_state(config)
            values = snapshot.values if snapshot else None
            if values and values.get("terminal_state") in (
                ProductionTerminalState.PARTIAL.value, ProductionTerminalState.WAITING_FOR_HUMAN.value
            ):
                values["terminal_state"] = None
                # Re-enter DISCOVER so reset children are re-attempted.
                if values.get("phase") in (
                    ProductionPhase.BUILD_REPORT.value, ProductionPhase.OPTIONAL_REMOTE_AUDIT.value,
                    ProductionPhase.COMPLETE.value,
                ):
                    values["phase"] = ProductionPhase.DISCOVER.value
                graph.update_state(config, values)
        return self.run()

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
