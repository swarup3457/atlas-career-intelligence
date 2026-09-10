"""Root LangGraph governor for the ten-company pilot (architecture s.11, prompt s.11).

ONE root graph owns the ten-company obligation. Company tasks fan out with native
LangGraph ``Send`` (at most two concurrent LLM agents, enforced by a bounded semaphore); a
company node owns its company to a terminal outcome — one same-company Sonnet retry with the
exact missing checklist, then at most one Opus escalation — so a retry never reruns the other
nine. Completed company results are persisted to a per-run partial store, so the SAME run
resumes after a process restart without repeating finished companies. The reducer waits for
all ten terminal results before evaluating, reporting, and validating. Checkpointing uses the
existing persistent SQLite saver (never ``MemorySaver``).
"""

from __future__ import annotations

import datetime
import json
import operator
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Callable, Optional
from typing_extensions import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from atlas.orchestration.checkpoints import open_checkpointer, thread_config
from atlas.pilot.config import PilotConfig
from atlas.pilot.discovery import CAREERS_ENTRY_HINTS, OFFICIAL_DOMAIN_HINTS
from atlas.pilot.evaluate import evaluate_pilot
from atlas.pilot.models import CompanySearchResult, CompanyStatus, LaneCoverage, SEARCHED_TERMINAL
from atlas.pilot.report import write_pilot_report
from atlas.pilot.usage import UsageMeter
from atlas.pilot.validator import validate_evaluation, validate_history
from atlas.pilot.worker import CompanyTask, LlmCompanySearchWorker

__all__ = ["PilotRuntime", "PilotOutcome", "run_pilot", "build_pilot_graph"]


class PilotState(TypedDict, total=False):
    pending: list[str]
    done: Annotated[list[str], operator.add]
    outcome: str


@dataclass
class PilotRuntime:
    config: PilotConfig
    worker: LlmCompanySearchWorker
    profile: object                      # CandidateSearchProfile
    output_root: Path
    run_id: str
    parent_run_id: Optional[str] = None
    escalation_model: str = "claude-opus-4.8"
    prior_manifest_path: Optional[Path] = None
    repo_root: Optional[Path] = None
    today: Optional[datetime.date] = None
    subdir: str = "llm_pilots"
    report_fn: Optional[Callable] = None
    domain_hints: dict = field(default_factory=dict)
    entry_hints: dict = field(default_factory=dict)
    usage: UsageMeter = field(default_factory=lambda: UsageMeter(label="pilot"))
    results: dict[str, CompanySearchResult] = field(default_factory=dict)
    _sem: threading.Semaphore = field(init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    report: object = field(default=None, init=False)
    validation: object = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._sem = threading.BoundedSemaphore(max(1, self.config.concurrency_company_agents))

    @property
    def partial_dir(self) -> Path:
        d = self.output_root / self.subdir / self.run_id / "_partial"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _partial_path(self, company: str) -> Path:
        import re
        return self.partial_dir / (re.sub(r"[^a-z0-9]+", "_", company.lower()).strip("_") + ".json")

    def load_partial(self, company: str) -> Optional[CompanySearchResult]:
        p = self._partial_path(company)
        if not p.exists():
            return None
        return _result_from_dict(json.loads(p.read_text(encoding="utf-8")))

    def save_partial(self, result: CompanySearchResult) -> None:
        self._partial_path(result.company).write_text(
            json.dumps(result.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )


@dataclass(frozen=True)
class PilotOutcome:
    run_id: str
    outcome: str
    companies_terminal: int
    india_jobs: int
    foreign_leads: int
    report: object
    validation: object


def _task_for(runtime: PilotRuntime, company: str, *, feedback: str = "", escalate: bool = False) -> CompanyTask:
    key = company.lower()
    return CompanyTask(
        company=company,
        task_id=f"{runtime.run_id}::{company.replace(' ', '_')}",
        domain_hint=runtime.domain_hints.get(key, OFFICIAL_DOMAIN_HINTS.get(key, "")),
        entry_hint=runtime.entry_hints.get(key, CAREERS_ENTRY_HINTS.get(key, "")),
        feedback=feedback, escalate=escalate,
    )


def _make_search_company(runtime: PilotRuntime):
    config = runtime.config

    def search_company(payload: dict) -> PilotState:
        company = payload["company"]
        # resume: skip a company already completed in a prior process for this run
        existing = runtime.load_partial(company)
        if existing is not None:
            with runtime._lock:
                runtime.results[company] = existing
            return {"done": [company]}

        with runtime._sem:  # enforce max-2 concurrent company agents
            usage_child = runtime.usage.child(company)
            # VALIDATE_COMPANY_RESULT + RETRY_INCOMPLETE_COMPANY (one same-company retry)
            result = runtime.worker.search_company(_task_for(runtime, company), usage_child)
            if not _company_ok(result, config) and config.max_search_rounds_per_company > 1:
                feedback = _missing_checklist(result, config)
                result = runtime.worker.search_company(
                    _task_for(runtime, company, feedback=feedback), usage_child
                )
                result.retries += 1
            # OPUS_ESCALATION_IF_REQUIRED (at most once, only for an unresolved company)
            if _needs_escalation(result, config) and config.max_escalations_per_company >= 1:
                esc_worker = _escalation_worker(runtime)
                escalated = esc_worker.search_company(
                    _task_for(runtime, company, feedback=_missing_checklist(result, config), escalate=True),
                    usage_child,
                )
                escalated.escalated = True
                if _company_ok(escalated, config) or not _company_ok(result, config):
                    result = escalated
            runtime.usage.merge(usage_child)

        with runtime._lock:
            runtime.results[company] = result
        runtime.save_partial(result)
        return {"done": [company]}

    return search_company


def _escalation_worker(runtime: PilotRuntime) -> LlmCompanySearchWorker:
    w = runtime.worker
    return LlmCompanySearchWorker(
        config=w.config, profile_summary=w.profile_summary, mode=w.mode,
        model=runtime.escalation_model, base_directory=w.base_directory,
        session_timeout_s=w.session_timeout_s, max_turns=w.max_turns,
        http_client_factory=w.http_client_factory,
    )


def _company_ok(result: CompanySearchResult, config: PilotConfig) -> bool:
    if result.status in SEARCHED_TERMINAL:
        return result.lane_checklist_complete(config.primary_lanes)
    # a truthful blocker is an acceptable terminal outcome (nothing more to do)
    return result.status in (
        CompanyStatus.ACCESS_LIMITED.value, CompanyStatus.AUTH_REQUIRED.value,
        CompanyStatus.UNSUPPORTED_SITE.value, CompanyStatus.NETWORK_UNAVAILABLE.value,
        CompanyStatus.WAITING_FOR_HUMAN.value,
    )


def _needs_escalation(result: CompanySearchResult, config: PilotConfig) -> bool:
    # escalate only a genuinely unresolved source (not a clean blocker, not a completed search)
    return result.status in (
        CompanyStatus.OFFICIAL_SOURCE_UNRESOLVED.value, CompanyStatus.FAILED.value,
    )


def _missing_checklist(result: CompanySearchResult, config: PilotConfig) -> str:
    missing = [
        lane for lane in config.primary_lanes
        if not (result.lanes.get(lane) and
                (result.lanes[lane].attempted or result.lanes[lane].board_snapshot_evaluated))
    ]
    parts = []
    if not result.career_entry_url:
        parts.append("resolve the official careers entry URL")
    if missing:
        parts.append(f"attempt these lanes: {', '.join(missing)}")
    if not result.evidence_urls:
        parts.append("capture at least one official evidence URL")
    return "; ".join(parts) or "re-verify the official source and submit a truthful terminal status"


def _make_start(runtime: PilotRuntime):
    def start(state: PilotState) -> PilotState:
        pending = list(runtime.config.companies)
        done = [c for c in pending if runtime.load_partial(c) is not None]
        return {"pending": pending, "done": []}

    return start


def _dispatch(state: PilotState):
    return [Send("search_company", {"company": c}) for c in state.get("pending", [])]


def _make_finalize(runtime: PilotRuntime):
    def finalize(state: PilotState) -> PilotState:
        # load every terminal company (from memory + partial store) in the sealed order
        results: list[CompanySearchResult] = []
        for c in runtime.config.companies:
            r = runtime.results.get(c) or runtime.load_partial(c)
            if r is None:
                r = CompanySearchResult(company=c, status=CompanyStatus.FAILED.value,
                                        lanes={l: LaneCoverage(lane=l) for l in runtime.config.primary_lanes})
            results.append(r)

        profile = runtime.profile
        from atlas.hunt.role_intent import load_role_intent_policy
        from atlas.policy.loader import load_policy

        intent = load_role_intent_policy()
        policy = load_policy()
        evaluation = evaluate_pilot(results, intent, policy, profile, today=runtime.today)

        searched = sum(1 for r in results if r.status in SEARCHED_TERMINAL)
        terminal = sum(1 for r in results if r.status)
        if evaluation.accepted:
            outcome = "PASS" if terminal >= len(runtime.config.companies) else "PARTIAL"
        elif searched >= len(runtime.config.companies):
            outcome = "COMPLETE_NO_MATCHES"
        elif terminal >= len(runtime.config.companies):
            outcome = "PARTIAL"
        else:
            outcome = "PARTIAL"

        usage_snapshot = runtime.usage.snapshot()
        candidate_prov = {"synthetic": getattr(profile, "synthetic", True),
                          "source_sha256": getattr(profile, "provenance_sha256", "")[:16],
                          "source_id": getattr(profile, "source_id", "")}
        if runtime.report_fn is not None:
            report = runtime.report_fn(runtime, evaluation, results, usage_snapshot, outcome, candidate_prov)
        else:
            report = write_pilot_report(
                evaluation, results, usage_snapshot, runtime.config,
                run_id=runtime.run_id, parent_run_id=runtime.parent_run_id,
                output_root=runtime.output_root, outcome=outcome, candidate_provenance=candidate_prov,
            )
        validation = validate_evaluation(
            evaluation, results, usage_snapshot, runtime.config,
            candidate_synthetic=bool(candidate_prov["synthetic"]), outcome=outcome,
        )
        if runtime.prior_manifest_path and runtime.repo_root:
            hist = validate_history(runtime.prior_manifest_path, runtime.repo_root)
            validation.checks.extend(hist.checks)
            validation.passed = validation.passed and hist.passed
        runtime.report = report
        runtime.validation = validation
        if not validation.passed and outcome == "PASS":
            outcome = "FAIL"
        return {"outcome": outcome}

    return finalize


def build_pilot_graph(runtime: PilotRuntime):
    builder = StateGraph(PilotState)
    builder.add_node("start", _make_start(runtime))
    builder.add_node("search_company", _make_search_company(runtime))
    builder.add_node("finalize", _make_finalize(runtime))
    builder.add_edge(START, "start")
    builder.add_conditional_edges("start", _dispatch, ["search_company"])
    builder.add_edge("search_company", "finalize")
    builder.add_edge("finalize", END)
    return builder


def run_pilot(runtime: PilotRuntime, *, checkpoint_db: Optional[Path] = None) -> PilotOutcome:
    builder = build_pilot_graph(runtime)
    db = checkpoint_db or (runtime.output_root / runtime.subdir / runtime.run_id / "pilot_checkpoints.sqlite")
    db.parent.mkdir(parents=True, exist_ok=True)
    with open_checkpointer(db) as saver:
        graph = builder.compile(checkpointer=saver)
        config = thread_config(runtime.run_id)
        config.setdefault("configurable", {})
        # cap real parallelism in addition to the in-node semaphore
        config["max_concurrency"] = runtime.config.concurrency_company_agents
        final = graph.invoke({}, config=config)
    report = runtime.report
    return PilotOutcome(
        run_id=runtime.run_id,
        outcome=final.get("outcome", ""),
        companies_terminal=sum(1 for c in runtime.config.companies
                               if (runtime.results.get(c) or runtime.load_partial(c))),
        india_jobs=(report.all_jobs_rows if report else 0),
        foreign_leads=(report.foreign_leads if report else 0),
        report=report,
        validation=runtime.validation,
    )


def reevaluate_from_partials(
    config: PilotConfig,
    profile: object,
    *,
    parent_run_dir: Path,
    output_root: Path,
    new_run_id: str,
    prior_manifest_path: Optional[Path] = None,
    repo_root: Optional[Path] = None,
    today: Optional[datetime.date] = None,
) -> PilotOutcome:
    """Re-run the DETERMINISTIC evaluation over a completed run's searched company results
    (child run reusing snapshots + measured usage; ZERO network / LLM cost)."""
    import re as _re

    from atlas.hunt.role_intent import load_role_intent_policy
    from atlas.policy.loader import load_policy

    partial_dir = parent_run_dir / "_partial"
    results: list[CompanySearchResult] = []
    for c in config.companies:
        slug = _re.sub(r"[^a-z0-9]+", "_", c.lower()).strip("_")
        p = partial_dir / f"{slug}.json"
        if p.exists():
            results.append(_result_from_dict(json.loads(p.read_text(encoding="utf-8"))))
        else:
            results.append(CompanySearchResult(
                company=c, status=CompanyStatus.FAILED.value,
                lanes={l: LaneCoverage(lane=l) for l in config.primary_lanes}))

    usage_path = parent_run_dir / "llm_usage.json"
    usage_snapshot = json.loads(usage_path.read_text(encoding="utf-8")) if usage_path.exists() else {"totals": {}, "by_model": {}}

    intent = load_role_intent_policy()
    policy = load_policy()
    evaluation = evaluate_pilot(results, intent, policy, profile, today=today)

    searched = sum(1 for r in results if r.status in SEARCHED_TERMINAL)
    terminal = sum(1 for r in results if r.status)
    if evaluation.accepted:
        outcome = "PASS" if terminal >= len(config.companies) else "PARTIAL"
    elif searched >= len(config.companies):
        outcome = "COMPLETE_NO_MATCHES"
    else:
        outcome = "PARTIAL"

    candidate_prov = {"synthetic": getattr(profile, "synthetic", True),
                      "source_sha256": getattr(profile, "provenance_sha256", "")[:16],
                      "source_id": getattr(profile, "source_id", "")}
    report = write_pilot_report(
        evaluation, results, usage_snapshot, config, run_id=new_run_id,
        parent_run_id=parent_run_dir.name, output_root=output_root, outcome=outcome,
        candidate_provenance=candidate_prov,
    )
    validation = validate_evaluation(
        evaluation, results, usage_snapshot, config,
        candidate_synthetic=bool(candidate_prov["synthetic"]), outcome=outcome,
    )
    if prior_manifest_path and repo_root:
        hist = validate_history(prior_manifest_path, repo_root)
        validation.checks.extend(hist.checks)
        validation.passed = validation.passed and hist.passed
    if not validation.passed and outcome == "PASS":
        outcome = "FAIL"
    # persist the corrected outcome in the manifest
    manifest_p = report.run_dir / "run_manifest.json"
    man = json.loads(manifest_p.read_text(encoding="utf-8"))
    man["outcome"] = outcome
    man["validation_passed"] = validation.passed
    manifest_p.write_text(json.dumps(man, indent=2, ensure_ascii=False), encoding="utf-8")
    return PilotOutcome(
        run_id=new_run_id, outcome=outcome,
        companies_terminal=terminal, india_jobs=report.all_jobs_rows,
        foreign_leads=report.foreign_leads, report=report, validation=validation,
    )


def _result_from_dict(d: dict) -> CompanySearchResult:
    from atlas.pilot.models import JobDetailEvidence, JobRejection

    lanes = {
        k: LaneCoverage(lane=k, attempted=v.get("attempted", False), queries=list(v.get("queries", [])),
                        pages=v.get("pages", 0), candidates=v.get("candidates", 0),
                        board_snapshot_evaluated=v.get("board_snapshot_evaluated", False))
        for k, v in d.get("lanes", {}).items()
    }
    jobs = [JobDetailEvidence(
        title=j.get("title", ""), company=j.get("company", d.get("company", "")), location=j.get("location", ""),
        work_mode=j.get("work_mode", ""), description=j.get("description", ""),
        mandatory_requirements=tuple(j.get("mandatory_requirements", [])),
        preferred_requirements=tuple(j.get("preferred_requirements", [])),
        experience_text=j.get("experience_text", ""), posted_date=j.get("posted_date", ""),
        updated_date=j.get("updated_date", ""), requisition_id=j.get("requisition_id", ""),
        official_url=j.get("official_url", ""), eligibility_text=j.get("eligibility_text", ""),
        source_family=j.get("source_family", ""), evidence_snippets=tuple(j.get("evidence_snippets", [])),
    ) for j in d.get("jobs", [])]
    rejections = [JobRejection(title=r.get("title", ""), lane=r.get("lane", ""),
                               reason_code=r.get("reason_code", ""), detail=r.get("detail", ""),
                               location=r.get("location", ""), url=r.get("url", ""))
                  for r in d.get("rejections", [])]
    return CompanySearchResult(
        company=d.get("company", ""), official_domain=d.get("official_domain", ""),
        career_entry_url=d.get("career_entry_url", ""), route=d.get("route", ""),
        source_family=d.get("source_family", ""), status=d.get("status", "FAILED"),
        lanes=lanes, jobs=jobs, rejections=rejections, limitations=list(d.get("limitations", [])),
        evidence_urls=list(d.get("evidence_urls", [])), model=d.get("model", ""), task_id=d.get("task_id", ""),
        tool_calls=d.get("tool_calls", 0), queries_attempted=list(d.get("queries_attempted", [])),
        pages_or_interactions=d.get("pages_or_interactions", 0), escalated=d.get("escalated", False),
        retries=d.get("retries", 0),
    )
