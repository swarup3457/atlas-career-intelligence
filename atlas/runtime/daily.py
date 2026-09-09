"""Daily operation runtime — driven by the ONE root daily LangGraph graph
(Phase 1E/F §5.1 / §11).

`DailyRunner` composes the whole daily pipeline (plan -> official/market
execution -> canonicalize -> triage -> deep evaluate -> build application packs
-> persist -> report -> publish latest) as checkpointed phases of a single
durable LangGraph graph. A crash resumes at the first nonterminal phase and
never reruns a completed phase; the runtime decides the truthful terminal state.

Offline/deterministic runs supply an in-memory job set and injected execution
callables; nothing here performs live network I/O by itself. The ranking,
application-pack, and output-contract stages are the REAL implementations.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from atlas.candidate.application_pack import ApplicationPackBuilder
from atlas.candidate.eligibility import CandidateProfile, RankableJob
from atlas.candidate.ranking import DeepEvaluator, TriageRanker
from atlas.config import Settings
from atlas.orchestration.checkpoints import open_checkpointer, thread_config
from atlas.orchestration.daily_graph import build_daily_graph, daily_is_terminal
from atlas.orchestration.daily_state import (
    DailyPhase,
    DailyState,
    DailyTerminalState,
    initial_daily_state,
)
from atlas.policy import load_policy
from atlas.reporting.production_output import (
    ProductionOutputPublisher,
    ProductionRunPaths,
    RunAlreadyPublishedError,
    recommendations_payload,
    sheet_data_from_evaluations,
)

JobProducer = Callable[[], Sequence[RankableJob]]


class AuthExpired(RuntimeError):
    """Raised by a live execution callable when a portal session needs a manual,
    visible re-auth. A scheduled/background run must NEVER prompt: the daily
    graph converts this into WAITING_FOR_HUMAN plus an exact manual command."""

    def __init__(self, family: str, manual_command: str) -> None:
        super().__init__(f"auth expired for {family}; run: {manual_command}")
        self.family = family
        self.manual_command = manual_command


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class DailyRunResult:
    def __init__(self, state: DailyState, runner: "DailyRunner") -> None:
        self.run_id = state.get("run_id", runner.run_id)
        self.terminal_state = state.get("terminal_state")
        self.phase = state.get("phase")
        self.status = state.get("status") or self.terminal_state
        self.jobs_discovered = state.get("jobs_discovered", 0)
        self.jobs_ranked = state.get("jobs_ranked", 0)
        self.selected = state.get("selected", 0)
        self.packs_built = state.get("packs_built", 0)
        self.report_valid = state.get("report_valid")
        self.latest_updated = state.get("latest_updated")
        self.run_dir = str(runner.paths.run_dir)
        self.workbook_path = str(runner.paths.workbook)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "terminal_state": self.terminal_state, "phase": self.phase,
            "status": self.status, "jobs_discovered": self.jobs_discovered,
            "jobs_ranked": self.jobs_ranked, "selected": self.selected,
            "packs_built": self.packs_built, "report_valid": self.report_valid,
            "latest_updated": self.latest_updated, "run_dir": self.run_dir,
            "workbook_path": self.workbook_path,
        }


class DailyRunner:
    def __init__(
        self,
        settings: Settings,
        run_id: str,
        *,
        jobs: Sequence[RankableJob],
        candidate: CandidateProfile,
        policy: Optional[Any] = None,
        controller: Optional[Any] = None,
        model: str = "none",
        triage_limit: int = 25,
        deep_limit: int = 10,
        official_exec: Optional[JobProducer] = None,
        market_exec: Optional[JobProducer] = None,
        source_health_provider: Optional[Any] = None,
        build_docx: bool = True,
        eligible_for_latest: bool = True,
        stop_after_phase: Optional[DailyPhase] = None,
        live: bool = False,
    ) -> None:
        self.settings = settings
        self.run_id = run_id
        self.thread_id = f"daily::{run_id}"
        self.jobs: list[RankableJob] = list(jobs)
        self.candidate = candidate
        self.policy = policy
        self.controller = controller
        self.model = model
        self.triage_limit = triage_limit
        self.deep_limit = deep_limit
        self.official_exec = official_exec
        self.market_exec = market_exec
        self.source_health_provider = source_health_provider
        self.build_docx = build_docx
        self.eligible_for_latest = eligible_for_latest
        self.stop_after_phase = stop_after_phase
        self.live = live

        self.publisher = ProductionOutputPublisher(settings)
        self.paths = ProductionRunPaths(self.publisher.root, run_id)

        # working data (recomputed deterministically each process; not checkpointed)
        self.jobs_by_key: dict[str, RankableJob] = {}
        self.result = None
        self.pack_results: list = []
        self.publish_result = None

    # -- phase-run counter (durable proof that completed phases never rerun) --
    def _phase_runs_path(self) -> Path:
        d = self.settings.logs_dir
        d.mkdir(parents=True, exist_ok=True)
        return d / f"daily_{self.run_id}_phase_runs.json"

    def _record_phase_run(self, phase: DailyPhase) -> None:
        p = self._phase_runs_path()
        try:
            counts = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except (ValueError, OSError):
            counts = {}
        counts[phase.value] = counts.get(phase.value, 0) + 1
        p.write_text(json.dumps(counts, indent=2, sort_keys=True), encoding="utf-8")

    def phase_run_counts(self) -> dict[str, int]:
        p = self._phase_runs_path()
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}

    # -- handlers -----------------------------------------------------------
    def _handlers(self) -> Mapping[DailyPhase, Callable[[DailyState, object], DailyState]]:
        return {
            DailyPhase.INITIALIZE: self._h_initialize,
            DailyPhase.LOAD_POLICY: self._h_load_policy,
            DailyPhase.LOAD_CANDIDATE: self._h_load_candidate,
            DailyPhase.PLAN: self._h_plan,
            DailyPhase.EXECUTE_OFFICIAL: self._h_execute_official,
            DailyPhase.EXECUTE_MARKET: self._h_execute_market,
            DailyPhase.CANONICALIZE: self._h_canonicalize,
            DailyPhase.TRIAGE: self._h_triage,
            DailyPhase.DEEP_EVALUATE: self._h_deep_evaluate,
            DailyPhase.BUILD_PACKS: self._h_build_packs,
            DailyPhase.PERSIST: self._h_persist,
            DailyPhase.BUILD_REPORT: self._h_build_report,
            DailyPhase.PUBLISH_LATEST: self._h_publish_latest,
        }

    def _h_initialize(self, state: DailyState, _rt) -> DailyState:
        self.paths.run_dir.mkdir(parents=True, exist_ok=True)
        self.paths.application_packs_dir.mkdir(parents=True, exist_ok=True)
        state["started_at"] = state.get("started_at") or _utcnow()
        self._record_phase_run(DailyPhase.INITIALIZE)
        return state

    def _h_load_policy(self, state: DailyState, _rt) -> DailyState:
        if self.policy is None:
            self.policy = load_policy()
        self._record_phase_run(DailyPhase.LOAD_POLICY)
        return state

    def _h_load_candidate(self, state: DailyState, _rt) -> DailyState:
        if self.candidate is None:
            raise RuntimeError("daily run requires a candidate profile")
        self._record_phase_run(DailyPhase.LOAD_CANDIDATE)
        return state

    def _h_plan(self, state: DailyState, _rt) -> DailyState:
        counters = state.setdefault("counters", {})
        counters["planned_jobs"] = len(self.jobs)
        self._record_phase_run(DailyPhase.PLAN)
        return state

    def _h_execute_official(self, state: DailyState, _rt) -> DailyState:
        try:
            if self.official_exec is not None:
                self.jobs.extend(self.official_exec())
        except AuthExpired as exc:
            return self._enter_waiting(state, DailyPhase.EXECUTE_OFFICIAL, exc)
        self._record_phase_run(DailyPhase.EXECUTE_OFFICIAL)
        return state

    def _h_execute_market(self, state: DailyState, _rt) -> DailyState:
        try:
            if self.market_exec is not None:
                self.jobs.extend(self.market_exec())
        except AuthExpired as exc:
            return self._enter_waiting(state, DailyPhase.EXECUTE_MARKET, exc)
        self._record_phase_run(DailyPhase.EXECUTE_MARKET)
        return state

    def _enter_waiting(self, state: DailyState, phase: DailyPhase, exc: "AuthExpired") -> DailyState:
        state.setdefault("notes", []).append(f"WAITING_FOR_HUMAN: {exc.manual_command}")
        state["status"] = DailyTerminalState.WAITING_FOR_HUMAN.value
        state["terminal_state"] = DailyTerminalState.WAITING_FOR_HUMAN.value
        self._record_phase_run(phase)
        return state

    def _h_canonicalize(self, state: DailyState, _rt) -> DailyState:
        # dedupe by job_key deterministically
        seen: dict[str, RankableJob] = {}
        for j in self.jobs:
            seen.setdefault(j.job_key, j)
        self.jobs = list(seen.values())
        self.jobs_by_key = seen
        # Durably persist the discovered lead set so a fresh-process RESUME
        # reconstructs the EXACT same jobs without re-running (non-deterministic)
        # live discovery.
        self._persist_leads()
        state["jobs_discovered"] = len(self.jobs)
        self._record_phase_run(DailyPhase.CANONICALIZE)
        return state

    def _h_triage(self, state: DailyState, _rt) -> DailyState:
        ranker = TriageRanker(self.policy, self.candidate, controller=self.controller,
                              model=self.model, triage_limit=self.triage_limit)
        triage = ranker.rank(self.jobs)
        state["jobs_ranked"] = len(triage.eligible)
        state["selected"] = len(triage.selected)
        self._triage = triage
        self._record_phase_run(DailyPhase.TRIAGE)
        return state

    def _h_deep_evaluate(self, state: DailyState, _rt) -> DailyState:
        from atlas.candidate.ranking import rank_and_evaluate

        self.result = rank_and_evaluate(
            self.jobs, self.policy, self.candidate, controller=self.controller,
            model=self.model, triage_limit=self.triage_limit, deep_limit=self.deep_limit,
        )
        state["selected"] = len(self.result.selected)
        self._record_phase_run(DailyPhase.DEEP_EVALUATE)
        return state

    def _ensure_pipeline(self) -> None:
        """Reconstruct the deterministic in-memory pipeline (dedup + ranking)
        if it is not already present. Pure and side-effect-free, so calling it
        on RESUME reproduces identical results WITHOUT rerunning any graph phase
        (the phase-run counters are untouched). For live runs, the discovered
        leads are reloaded from the durable per-run leads file so resume is exact
        despite non-deterministic live discovery."""
        if not self.jobs_by_key:
            if not self.jobs:
                self.jobs = self._load_leads()
            seen: dict[str, RankableJob] = {}
            for j in self.jobs:
                seen.setdefault(j.job_key, j)
            self.jobs = list(seen.values())
            self.jobs_by_key = seen
        if self.result is None:
            from atlas.candidate.ranking import rank_and_evaluate

            self.result = rank_and_evaluate(
                self.jobs, self.policy, self.candidate, controller=self.controller,
                model=self.model, triage_limit=self.triage_limit, deep_limit=self.deep_limit,
            )

    # -- durable lead persistence (exact live resume) -----------------------
    def _leads_path(self) -> Path:
        self.paths.run_dir.mkdir(parents=True, exist_ok=True)
        return self.paths.run_dir / "discovered_leads.json"

    @staticmethod
    def _job_to_row(j: RankableJob) -> dict[str, Any]:
        return {
            "job_key": j.job_key, "company": j.company, "title": j.title, "location": j.location,
            "lane": j.lane, "work_mode": j.work_mode, "description": j.description,
            "mandatory_requirements": list(j.mandatory_requirements),
            "preferred_requirements": list(j.preferred_requirements),
            "experience_text": j.experience_text, "eligibility_text": j.eligibility_text,
            "posted_date": j.posted_date.isoformat() if j.posted_date else None,
            "verification_state": j.verification_state, "has_live_official_page": j.has_live_official_page,
            "is_fetchable": j.is_fetchable, "source_family": j.source_family,
            "canonical_id": j.canonical_id, "url": j.url,
        }

    @staticmethod
    def _row_to_job(row: dict[str, Any]) -> RankableJob:
        pd = row.get("posted_date")
        posted = datetime.date.fromisoformat(pd) if pd else None
        return RankableJob(
            job_key=row["job_key"], company=row.get("company", ""), title=row.get("title", ""),
            location=row.get("location", ""), lane=row.get("lane"), work_mode=row.get("work_mode", "UNKNOWN"),
            description=row.get("description", ""),
            mandatory_requirements=tuple(row.get("mandatory_requirements", [])),
            preferred_requirements=tuple(row.get("preferred_requirements", [])),
            experience_text=row.get("experience_text", ""), eligibility_text=row.get("eligibility_text", ""),
            posted_date=posted, verification_state=row.get("verification_state", "PORTAL_CURRENT_LEAD"),
            has_live_official_page=bool(row.get("has_live_official_page", False)),
            is_fetchable=bool(row.get("is_fetchable", True)), source_family=row.get("source_family", ""),
            canonical_id=row.get("canonical_id"), url=row.get("url"),
        )

    def _persist_leads(self) -> None:
        rows = [self._job_to_row(j) for j in self.jobs]
        tmp = self._leads_path().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rows, indent=2, sort_keys=True, default=str), encoding="utf-8")
        import os as _os

        _os.replace(tmp, self._leads_path())

    def _load_leads(self) -> list[RankableJob]:
        p = self._leads_path()
        if not p.exists():
            return list(self.jobs)
        try:
            rows = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return list(self.jobs)
        return [self._row_to_job(r) for r in rows]

    def _h_build_packs(self, state: DailyState, _rt) -> DailyState:
        self._ensure_pipeline()
        builder = ApplicationPackBuilder(self.candidate, controller=self.controller, write_docx=self.build_docx)
        built = 0
        self.pack_results = []
        for key in self.result.selected:
            job = self.jobs_by_key.get(key)
            evaluation = self.result.by_key(key)
            if job is None or evaluation is None:
                continue
            # only build packs for apply-family recommendations
            if evaluation.recommendation not in ("PRIORITY_APPLY", "STRONG_APPLY", "APPLY_AFTER_TAILORING"):
                continue
            try:
                res = builder.build(job, evaluation, packs_root=self.paths.application_packs_dir)
                self.pack_results.append(res)
                built += 1
            except Exception as exc:  # noqa: BLE001 - one pack failure is not fatal to the run
                state.setdefault("notes", []).append(f"pack failed for {key}: {exc}")
        state["packs_built"] = built
        self._record_phase_run(DailyPhase.BUILD_PACKS)
        return state

    def _h_persist(self, state: DailyState, _rt) -> DailyState:
        # durable side payloads are written by the publisher at BUILD_REPORT;
        # this phase is a checkpoint boundary that records compact counts.
        self._record_phase_run(DailyPhase.PERSIST)
        return state

    def _h_build_report(self, state: DailyState, _rt) -> DailyState:
        self._ensure_pipeline()
        evaluations = list(self.result.evaluations)
        sheet_data = sheet_data_from_evaluations(evaluations, self.jobs_by_key)
        relevant = sum(1 for e in evaluations
                       if e.recommendation in ("PRIORITY_APPLY", "STRONG_APPLY", "APPLY_AFTER_TAILORING"))
        sheet_data["Run_Summary"] = [{
            "Run_ID": self.run_id, "Run_Date": (state.get("started_at") or _utcnow())[:10],
            "Run_Status": "COMPLETE", "Raw_Discoveries": len(self.jobs),
            "Relevant_Discoveries": relevant, "Remaining_Work": "",
        }]
        status = "COMPLETE"
        try:
            source_health = {"mode": "live" if self.live else "offline"}
            if self.source_health_provider is not None:
                try:
                    extra = self.source_health_provider()
                    if isinstance(extra, dict):
                        source_health.update(extra)
                except Exception as exc:  # noqa: BLE001
                    source_health["provider_error"] = str(exc)
            self.publish_result = self.publisher.publish(
                self.run_id, status=status, sheet_data=sheet_data,
                coverage={"jobs": len(self.jobs)},
                source_health=source_health,
                recommendations=recommendations_payload(evaluations),
                portal_leads={"count": 0},
                verification_summary={"selected": len(self.result.selected)},
                manifest_extra={"packs": [p.to_dict() for p in self.pack_results]},
                eligible_for_latest=self.eligible_for_latest,
            )
            state["report_valid"] = self.publish_result.report_valid
            state["status"] = self.publish_result.status
        except RunAlreadyPublishedError:
            # idempotent resume: the report was already published successfully
            manifest = self.publisher.show_run(self.run_id) or {}
            state["report_valid"] = manifest.get("report_valid", True)
            state["status"] = manifest.get("status", "COMPLETE")
        self._record_phase_run(DailyPhase.BUILD_REPORT)
        return state

    def _h_publish_latest(self, state: DailyState, _rt) -> DailyState:
        if self.publish_result is not None:
            state["latest_updated"] = self.publish_result.latest_updated
        else:
            latest = self.publisher.latest() or {}
            state["latest_updated"] = latest.get("run_id") == self.run_id
        self._record_phase_run(DailyPhase.PUBLISH_LATEST)
        return state

    # -- terminal resolution ------------------------------------------------
    def resolve_terminal(self, state: DailyState) -> DailyTerminalState:
        if state.get("status") == "COMPLETE" and state.get("report_valid"):
            return DailyTerminalState.COMPLETE
        return DailyTerminalState.PARTIAL

    # -- graph driver -------------------------------------------------------
    def _run_graph(self) -> DailyState:
        builder = build_daily_graph(self._handlers(), self)
        with open_checkpointer(self.settings.checkpoint_db) as checkpointer:
            graph = builder.compile(checkpointer=checkpointer)
            config = thread_config(self.thread_id)
            snapshot = graph.get_state(config)
            existing = dict(snapshot.values) if snapshot and snapshot.values else {}
            if not existing:
                first_input: DailyState = initial_daily_state(self.run_id, live=self.live, started_at=_utcnow())
                state: DailyState = {}
            else:
                first_input = {}
                state = existing
            invoked_first = False
            while not daily_is_terminal(state):
                phase_before = state.get("phase")
                payload = first_input if not invoked_first else {}
                invoked_first = True
                state = graph.invoke(payload, config)
                if self.stop_after_phase is not None and phase_before == self.stop_after_phase.value:
                    break
            return state

    # -- public API ---------------------------------------------------------
    def plan(self) -> dict[str, Any]:
        if self.policy is None:
            self.policy = load_policy()
        return {
            "run_id": self.run_id,
            "jobs": len(self.jobs),
            "triage_limit": self.triage_limit,
            "deep_limit": self.deep_limit,
            "run_dir": str(self.paths.run_dir),
            "workbook_path": str(self.paths.workbook),
            "production_root": str(self.publisher.root),
        }

    def run(self) -> DailyRunResult:
        if self.policy is None:
            self.policy = load_policy()
        state = self._run_graph()
        return DailyRunResult(state, self)

    def resume(self) -> DailyRunResult:
        if self.policy is None:
            self.policy = load_policy()
        self.stop_after_phase = None
        state = self._run_graph()
        return DailyRunResult(state, self)

    def status(self) -> dict[str, Any]:
        builder = build_daily_graph(self._handlers(), self)
        with open_checkpointer(self.settings.checkpoint_db) as checkpointer:
            graph = builder.compile(checkpointer=checkpointer)
            snapshot = graph.get_state(thread_config(self.thread_id))
            state = dict(snapshot.values) if snapshot and snapshot.values else {}
        manifest = self.publisher.show_run(self.run_id)
        return {
            "run_id": self.run_id,
            "phase": state.get("phase"),
            "terminal_state": state.get("terminal_state"),
            "phases_completed": state.get("phases_completed", []),
            "report_valid": state.get("report_valid"),
            "latest_updated": state.get("latest_updated"),
            "manifest_status": (manifest or {}).get("status"),
            "run_dir": str(self.paths.run_dir),
        }


__all__ = ["DailyRunner", "DailyRunResult", "JobProducer", "AuthExpired"]
