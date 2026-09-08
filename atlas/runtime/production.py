"""Atlas production search runtime (Phase 1B, build spec 7.11/7.12/7.16).

A clean, generic production runtime that is DISTINCT from the demo
:class:`atlas.runtime.engine.AtlasRuntime` (which is preserved as a
regression/demo tool). It drives the explicit multi-phase production graph
with fake/fixture discovery only — no real adapter, no live web search.

Guarantees exercised end-to-end with :class:`NullController`:
    * phases run in the fixed order and search is isolated from persistence;
    * detailed discoveries live in SQLite while the checkpoint stays compact;
    * PARTIAL / WAITING_FOR_HUMAN / FAILED are distinct from COMPLETE;
    * a remote-audit failure degrades to REMOTE_AUDIT_DEGRADED without
      stopping the local run;
    * the completed manifest has a populated, timezone-aware UTC completed_at.
"""

from __future__ import annotations

import datetime
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from atlas.candidate.importer import build_synthetic_ledger
from atlas.config import Settings
from atlas.controllers.base import NullController
from atlas.controllers.operations import (
    CandidateMatchRequest,
    RoleClassificationRequest,
    TypedControllerOps,
    VerificationReviewRequest,
)
from atlas.orchestration.checkpoints import open_checkpointer, thread_config
from atlas.orchestration.graph import RUN_STATUS_COMPLETE  # noqa: F401 (parity import)
from atlas.orchestration.production_graph import (
    PhaseContext,
    build_production_graph,
)
from atlas.orchestration.production_state import (
    ProductionPhase,
    ProductionState,
    ProductionTerminalState,
    bump,
    checkpoint_size_bytes,
    initial_production_state,
)
from atlas.persistence.remote_audit import (
    NullRemoteAudit,
    RemoteAuditResult,
    export_run_audit,
)
from atlas.persistence.sqlite import StateStore
from atlas.planning import CoveragePlanner, PlanInput, PlannedCompany
from atlas.policy import load_policy
from atlas.policy.rules import VerificationInput, classify_verification
from atlas.reporting.mapping import load_report_mapping, validate_report, write_report


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


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
    report_path: Optional[str] = None
    report_valid: Optional[bool] = None
    remote_audit: Optional[RemoteAuditResult] = None
    persistence_side_effects: tuple[str, ...] = ()
    manifest: dict = field(default_factory=dict)


class ProductionSearchRuntime:
    def __init__(
        self,
        settings: Settings,
        run_id: str,
        *,
        policy_dir: Optional[Path] = None,
        controller=None,
        discovery_count: int = 25,
        companies: Optional[list[PlannedCompany]] = None,
        simulate_waiting_for_human: bool = False,
        simulate_plan_failure: bool = False,
        remote_audit=None,
        remote_audit_enabled: bool = False,
        stop_after_phase: Optional[ProductionPhase] = None,
        write_report: bool = True,
        report_path: Optional[Path] = None,
    ):
        self.settings = settings
        self.run_id = run_id
        self.thread_id = f"prod::{run_id}"
        self.policy_dir = policy_dir
        self.ops = TypedControllerOps(controller or NullController(), model=getattr(settings, "controller", "none"))
        self.discovery_count = discovery_count
        self.companies = companies
        self.simulate_waiting_for_human = simulate_waiting_for_human
        self.simulate_plan_failure = simulate_plan_failure
        self.remote_audit = remote_audit or NullRemoteAudit()
        self.remote_audit_enabled = remote_audit_enabled
        self.stop_after_phase = stop_after_phase
        self.write_report = write_report
        self.report_path = report_path

        self.ctx = PhaseContext()
        self.policy = None
        self.ledger = None
        self.plan = None
        self.remote_audit_result: Optional[RemoteAuditResult] = None
        self._report_written: Optional[str] = None
        self._report_valid: Optional[bool] = None
        self._started_at = _utcnow()

    # -- phase handlers -----------------------------------------------------
    def _h_initialize(self, state: ProductionState, _rt) -> ProductionState:
        bump(state, "initialized", 1)
        return state

    def _h_load_profile(self, state: ProductionState, _rt) -> ProductionState:
        # Private candidate evidence is loaded into memory only; the checkpoint
        # records a COUNT, never the claims/PII. Uses the synthetic (PII-free)
        # ledger; a real run imports the private profile into gitignored storage.
        self.ledger = build_synthetic_ledger()
        state.setdefault("counters", {})["candidate_claims"] = len(self.ledger.claims())
        state["counters"]["unresolved_conflicts"] = len(self.ledger.unresolved_conflicts())
        return state

    def _h_load_policy(self, state: ProductionState, _rt) -> ProductionState:
        self.policy = load_policy(self.policy_dir)
        state["policy_fingerprint"] = self.policy.short_fingerprint
        return state

    def _h_build_plan(self, state: ProductionState, _rt) -> ProductionState:
        if self.simulate_plan_failure:
            state["terminal_state"] = ProductionTerminalState.FAILED.value
            state.setdefault("notes", []).append("plan build failed")
            return state
        lanes = list(self.policy.lanes.keys()) if self.policy else []
        companies = self.companies or [
            PlannedCompany(company_id=f"co{i}", name=n, tier="A", mode="DELTA",
                           source_instances=(f"co{i}-workday",), geography_group="PRIMARY")
            for i, n in enumerate(self.policy.company_seed.names()[:10])
        ]
        plan_input = PlanInput(
            run_id=self.run_id, companies=companies,
            portal_families=["linkedin", "naukri"], ats_families=["workday", "greenhouse"],
            lanes=lanes, geography_groups=["PRIMARY", "SECONDARY"],
            policy_fingerprint=state.get("policy_fingerprint", ""),
        )
        self.plan = CoveragePlanner().build_and_seal(plan_input)
        state["plan_fingerprint"] = self.plan.sealed_fingerprint or ""
        # Only task IDs (compact) are checkpointed — never full task payloads.
        state["task_ids"] = [t.coverage_id for t in self.plan.tasks()][:2000]
        state.setdefault("counters", {})["planned_tasks"] = len(self.plan.tasks())
        # Persisting the plan lifecycle is a persistence phase concern; here we
        # only record it in memory (search-first isolation).
        return state

    def _h_discover(self, state: ProductionState, _rt) -> ProductionState:
        # Synthetic discovery. Detailed observations persist to SQLite; ONLY a
        # counter enters the checkpoint (keeps it bounded, build spec 7.10).
        with StateStore(self.settings.state_db) as store:
            for i in range(self.discovery_count):
                cid = f"job::{self.run_id}::{i}"
                store.upsert_canonical_job(
                    cid, company=f"Company {i % 10}", job_id=str(i), role="Software Engineer",
                    location="Bengaluru", status="UNKNOWN",
                    content_hash=hashlib.sha256(cid.encode()).hexdigest(),
                    payload={"synthetic": True, "lane": "JAVA_BACKEND"},
                )
        bump(state, "discovered", self.discovery_count)
        return state

    def _h_health_gate(self, state: ProductionState, _rt) -> ProductionState:
        bump(state, "health_checked", 1)
        return state

    def _h_detail_hydration(self, state: ProductionState, _rt) -> ProductionState:
        hydrated = min(state.get("counters", {}).get("discovered", 0), 5)
        bump(state, "hydrated", hydrated)
        return state

    def _h_verification(self, state: ProductionState, _rt) -> ProductionState:
        # Deterministic verification of a representative specific-role page.
        level = classify_verification(
            VerificationInput(page_kind="specific_role_page", identity_aligned=True, current_content=True)
        )
        # Optional typed reviewer (NullController => deterministic default).
        self.ops.review_verification(
            VerificationReviewRequest(page_kind="specific_role_page", identity_aligned=True, current_content=True)
        )
        bump(state, "verified", 1)
        state.setdefault("counters", {})[f"level_{level.value.lower()}"] = state["counters"].get(
            f"level_{level.value.lower()}", 0
        ) + 1
        return state

    def _h_dedupe(self, state: ProductionState, _rt) -> ProductionState:
        bump(state, "unique_after_dedupe", state.get("counters", {}).get("discovered", 0))
        return state

    def _h_match(self, state: ProductionState, _rt) -> ProductionState:
        self.ops.classify_role(RoleClassificationRequest(title="Software Engineer", lane_hint="JAVA_BACKEND"))
        self.ops.match_candidate(
            CandidateMatchRequest(job_requirements=("Java", "Spring Boot"), supported_evidence=("Java",))
        )
        bump(state, "matched", 1)
        return state

    def _h_persist_local(self, state: ProductionState, _rt) -> ProductionState:
        self.ctx.record_side_effect(ProductionPhase.PERSIST_LOCAL, "sqlite_persist")
        with StateStore(self.settings.state_db) as store:
            if store.get_run(self.run_id) is None:
                store.create_run(self.run_id, controller=getattr(self.settings, "controller", "none"),
                                 metadata={"production": True})
            if self.plan is not None:
                store.upsert_coverage_plan(
                    self.run_id, self.plan.state.value,
                    no_work_due=self.plan.no_work_due, fingerprint=self.plan.sealed_fingerprint,
                    policy_fingerprint=state.get("policy_fingerprint", ""),
                )
        bump(state, "persisted", 1)
        return state

    def _h_build_report(self, state: ProductionState, _rt) -> ProductionState:
        if self.simulate_waiting_for_human:
            # A human-waiting task must NOT make the run look COMPLETE.
            state["terminal_state"] = ProductionTerminalState.WAITING_FOR_HUMAN.value
            state.setdefault("human_waiting", []).append("manual_verification_review")
            return state
        self.ctx.record_side_effect(ProductionPhase.BUILD_REPORT, "excel_report")
        if self.write_report:
            mapping = load_report_mapping()
            out = Path(self.report_path) if self.report_path else (self.settings.output_dir / f"Atlas_Jobs_{self.run_id}.xlsx")
            data = self._synthetic_report_data(state)
            write_report(mapping, out, data)
            validation = validate_report(mapping, out)
            self._report_written = str(out)
            self._report_valid = validation.ok
        bump(state, "reported", 1)
        return state

    def _h_remote_audit(self, state: ProductionState, _rt) -> ProductionState:
        self.ctx.record_side_effect(ProductionPhase.OPTIONAL_REMOTE_AUDIT, "remote_audit")
        events = [{"type": "run_completed", "run_id": self.run_id, "counters": dict(state.get("counters", {}))}]
        self.remote_audit_result = export_run_audit(
            self.remote_audit, self.run_id, events, enabled=self.remote_audit_enabled
        )
        state.setdefault("notes", []).append(self.remote_audit_result.status)
        return state

    def _synthetic_report_data(self, state: ProductionState) -> dict:
        counters = state.get("counters", {})
        return {
            "All_Jobs": [
                {
                    "company": "Company 0", "role_title": "Software Engineer", "source_job_id": "0",
                    "location": "Bengaluru", "lane": "JAVA_BACKEND",
                    "official_apply_url": "https://example.invalid/careers/0",
                    "verification_level": "VERIFIED_OFFICIAL", "job_lifecycle_status": "ACTIVE",
                    "recommendation": "STRONG_APPLY", "freshness_band": "0-7 days",
                }
            ],
            "Run_Summary": [
                {
                    "Run_ID": self.run_id, "Run_Status": state.get("terminal_state", "RUNNING"),
                    "Raw_Discoveries": counters.get("discovered", 0),
                    "Relevant_Discoveries": counters.get("unique_after_dedupe", 0),
                    "Verified_Official": counters.get("level_verified_official", 0),
                }
            ],
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
    def run(self) -> ProductionRunResult:
        handlers = self._handlers()
        graph_builder = build_production_graph(handlers, self)
        with open_checkpointer(self.settings.checkpoint_db) as checkpointer:
            graph = graph_builder.compile(checkpointer=checkpointer)
            config = thread_config(self.thread_id)
            existing = graph.get_state(config).values
            state: ProductionState = existing if existing else initial_production_state(self.run_id)

            max_steps = 100
            steps = 0
            while not state.get("terminal_state") and steps < max_steps:
                steps += 1
                phase_before = state.get("phase")
                state = graph.invoke({}, config)
                # Partial stop: honor stop_after_phase (exact continuation is in
                # the checkpoint — the next phase pointer).
                if (
                    self.stop_after_phase is not None
                    and phase_before == self.stop_after_phase.value
                    and not state.get("terminal_state")
                ):
                    # The checkpoint already advanced to the next phase (exact
                    # continuation); report PARTIAL without writing a terminal
                    # into the checkpoint so resume() can continue.
                    return self._finalize(state, ProductionTerminalState.PARTIAL.value)

            terminal = state.get("terminal_state") or ProductionTerminalState.PARTIAL.value
            return self._finalize(state, terminal)

    def resume(self) -> ProductionRunResult:
        # Clear a PARTIAL terminal so the checkpointed run can continue.
        with open_checkpointer(self.settings.checkpoint_db) as checkpointer:
            graph = build_production_graph(self._handlers(), self).compile(checkpointer=checkpointer)
            config = thread_config(self.thread_id)
            values = graph.get_state(config).values or initial_production_state(self.run_id)
            if values.get("terminal_state") == ProductionTerminalState.PARTIAL.value:
                values["terminal_state"] = None
                graph.update_state(config, values)
        return self.run()

    def _finalize(self, state: ProductionState, terminal: str) -> ProductionRunResult:
        completed_at = _utcnow()
        manifest = {
            "run_id": self.run_id,
            "status": terminal,
            "started_at": self._started_at.isoformat(),
            "completed_at": completed_at.isoformat(),  # populated + tz-aware UTC
            "policy_fingerprint": state.get("policy_fingerprint", ""),
            "plan_fingerprint": state.get("plan_fingerprint", ""),
            "phases_completed": list(state.get("phases_completed", [])),
            "counters": dict(state.get("counters", {})),
        }
        # Record run status in SQLite (terminal is truthful, never faux COMPLETE).
        try:
            with StateStore(self.settings.state_db) as store:
                if store.get_run(self.run_id) is None:
                    store.create_run(self.run_id, controller=getattr(self.settings, "controller", "none"),
                                     metadata={"production": True})
                store.complete_run(self.run_id, status=terminal)
        except Exception:  # noqa: BLE001 - persistence failure must not crash finalize
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
            report_path=self._report_written,
            report_valid=self._report_valid,
            remote_audit=self.remote_audit_result,
            persistence_side_effects=tuple(self.ctx.persistence_side_effects),
            manifest=manifest,
        )


__all__ = ["ProductionSearchRuntime", "ProductionRunResult"]
