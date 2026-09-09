"""Root LangGraph Hunt governor (build spec 14, architecture s.9).

ONE root graph owns completion. Company collection + qualification fan out with
native LangGraph ``Send``; a checkpointed fan-in reducer decides whether to seal
another immutable extension batch (no early stop) and, when all sealed
obligations are terminal, routes to report + validate. The LLM never declares
coverage complete — the graph does, from the sealed company x lane obligations.

Heavy data (snapshots, details, decisions) lives in the runtime accumulator and
in immutable artifacts; the checkpointed state stays compact (counters + ids),
matching the existing Atlas production-graph discipline.
"""

from __future__ import annotations

import datetime
import operator
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Optional
from typing_extensions import TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from atlas.hunt.campaign import CompanyCampaign, CompanyRef, extend_campaign
from atlas.hunt.matching import CandidateMatchDecision, HuntCandidate, diversify_shortlist, match_qualified
from atlas.hunt.models import RunLineage, SourceCoverage
from atlas.hunt.pipeline import BoardProvider, HuntPipelineResult, evaluate_detail
from atlas.hunt.prefilter import hydration_union, prefilter_snapshot
from atlas.hunt.qualification import QualificationStatus
from atlas.hunt.report import HuntReport, write_hunt_report
from atlas.hunt.role_intent import RoleIntentPolicy
from atlas.hunt.validator import ValidationReport, validate_run_dir
from atlas.policy.loader import PolicyBundle

__all__ = ["HuntRuntime", "HuntOutcome", "build_hunt_graph", "run_hunt"]

_MAX_EXTENSION_ROUNDS = 3


class HuntState(TypedDict, total=False):
    pending: list[str]
    round: int
    done_companies: Annotated[list[str], operator.add]
    outcome: str
    report_path: str
    relevant: int


@dataclass
class HuntRuntime:
    intent: RoleIntentPolicy
    policy: PolicyBundle
    provider: BoardProvider
    candidate: HuntCandidate
    campaign: CompanyCampaign
    output_root: Path
    run_id: str
    lineage: RunLineage
    candidate_years: Optional[float] = None
    today: Optional[datetime.date] = None
    allow_extension: bool = True
    result: HuntPipelineResult = field(init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _source_seen: dict = field(default_factory=dict, init=False)
    _processed: set = field(default_factory=set, init=False)
    _extensions: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.result = HuntPipelineResult(campaign_id=self.campaign.campaign_id, lanes=self.campaign.lanes)

    def company_ref(self, name: str) -> CompanyRef:
        for c in self.campaign.companies:
            if c.name == name:
                return c
        return CompanyRef(name=name, group="", tier="A")


@dataclass(frozen=True)
class HuntOutcome:
    run_id: str
    outcome: str
    report: Optional[HuntReport]
    validation: Optional[ValidationReport]
    relevant_jobs: int
    qualified_jobs: int
    companies: int
    obligations: int


def _make_process_company(runtime: HuntRuntime):
    from atlas.hunt.pipeline import _coverage_rows

    def process_company(payload: dict) -> HuntState:
        name = payload["name"]
        company = runtime.company_ref(name)
        with runtime._lock:
            if name in runtime._processed:
                return {"done_companies": [name]}
            runtime._processed.add(name)
        snapshot = runtime.provider.fetch_board(runtime.campaign.campaign_id, company)
        prefilter = prefilter_snapshot(snapshot, runtime.intent)
        union = hydration_union(prefilter)
        details = []
        for sid in union:
            d = runtime.provider.hydrate(snapshot, sid)
            if d is not None:
                details.append(d)
        evaluations = [
            evaluate_detail(d, runtime.intent, runtime.policy, candidate_years=runtime.candidate_years,
                            overall_evidence_strong=runtime.candidate.strong_overall, today=runtime.today)
            for d in details
        ]
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        cov = _coverage_rows(company, snapshot, runtime.campaign.lanes, prefilter, evaluations, checked_at=now_iso)
        matches = [
            match_qualified(ev.detail, ev.qualification.by_lane[ev.final_lane], runtime.candidate, today=runtime.today)
            for ev in evaluations
            if ev.final_status == QualificationStatus.QUALIFIED.value and ev.final_lane
        ]
        with runtime._lock:
            runtime.result.snapshots.append(snapshot)
            runtime.result.details.extend(details)
            runtime.result.evaluations.extend(evaluations)
            runtime.result.coverage.extend(cov)
            runtime.result.matches.extend(matches)
            sc = runtime._source_seen.get(snapshot.source_instance_id)
            if sc is None:
                sc = SourceCoverage(
                    source_instance_id=snapshot.source_instance_id, source_family=snapshot.source_family,
                    route=snapshot.route_family, health=snapshot.source_health,
                    adapter_version=snapshot.adapter_version, parser_version=snapshot.parser_version,
                    limitation=snapshot.access_status if snapshot.access_status != "OK" else "",
                )
                runtime._source_seen[snapshot.source_instance_id] = sc
            sc.pages += snapshot.pages
            sc.request_count += 1
            sc.raw_jobs += snapshot.raw_job_count
            sc.companies += 1
            sc.qualified_jobs += sum(1 for ev in evaluations if ev.final_status == QualificationStatus.QUALIFIED.value)
        return {"done_companies": [name]}

    return process_company


def _make_start(runtime: HuntRuntime):
    def start(state: HuntState) -> HuntState:
        batch0 = [c.name for c in runtime.campaign.batches[0].companies]
        return {"pending": batch0, "round": 0}

    return start


def _route_dispatch(state: HuntState):
    return [Send("process_company", {"name": n}) for n in state.get("pending", [])]


def _make_collect(runtime: HuntRuntime):
    def collect(state: HuntState) -> HuntState:
        rnd = state.get("round", 0)
        runtime.result.source_coverage = list(runtime._source_seen.values())
        relevant = len(runtime.result.matches)
        return {"relevant": relevant, "round": rnd}

    return collect


def _make_route_after_collect(runtime: HuntRuntime):
    def route_after_collect(state: HuntState):
        relevant = state.get("relevant", 0)
        rnd = state.get("round", 0)
        # No early stop: if nothing relevant yet, budget remains, and extension
        # is allowed, seal an immutable extension batch and fan it out.
        if (
            runtime.allow_extension
            and relevant == 0
            and runtime._extensions < _MAX_EXTENSION_ROUNDS
            and runtime.campaign.company_count < runtime.campaign.max_batch
        ):
            batch = extend_campaign(runtime.campaign, runtime.policy, now=None)
            if batch is not None and batch.companies:
                runtime._extensions += 1
                names = [c.name for c in batch.companies]
                return [Send("process_company", {"name": n, "_round": rnd + 1}) for n in names]
        return "finalize"

    return route_after_collect


def _make_finalize(runtime: HuntRuntime):
    def finalize(state: HuntState) -> HuntState:
        # Re-run collect fold for any extension companies, then decide outcome.
        runtime.result.source_coverage = list(runtime._source_seen.values())
        shortlist = diversify_shortlist(runtime.result.matches, runtime.intent)
        if shortlist:
            outcome = "ENGINEERING_PASS_WITH_MATCHES"
        else:
            outcome = "ENGINEERING_PASS_COMPLETE_NO_MATCHES"
        report = write_hunt_report(
            runtime.result, runtime.campaign, shortlist, runtime.lineage,
            runtime.intent, runtime.policy, run_id=runtime.run_id,
            output_root=runtime.output_root, outcome=outcome,
        )
        runtime.lineage = RunLineage(**{**vars(runtime.lineage), "terminal_status": outcome,
                                        "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat()})
        runtime._report = report  # type: ignore[attr-defined]
        return {"outcome": outcome, "report_path": str(report.workbook_path), "relevant": len(shortlist)}

    return finalize


def _make_validate(runtime: HuntRuntime):
    def validate(state: HuntState) -> HuntState:
        report: HuntReport = getattr(runtime, "_report")
        vr = validate_run_dir(report.run_dir, intent=runtime.intent)
        runtime._validation = vr  # type: ignore[attr-defined]
        outcome = state.get("outcome", "")
        if not vr.passed:
            outcome = "FAILED"
        return {"outcome": outcome}

    return validate


def build_hunt_graph(runtime: HuntRuntime):
    builder = StateGraph(HuntState)
    builder.add_node("start", _make_start(runtime))
    builder.add_node("process_company", _make_process_company(runtime))
    builder.add_node("collect", _make_collect(runtime))
    builder.add_node("finalize", _make_finalize(runtime))
    builder.add_node("validate", _make_validate(runtime))

    builder.add_edge(START, "start")
    builder.add_conditional_edges("start", _route_dispatch, ["process_company"])
    builder.add_edge("process_company", "collect")
    builder.add_conditional_edges("collect", _make_route_after_collect(runtime), ["process_company", "finalize"])
    builder.add_edge("finalize", "validate")
    builder.add_edge("validate", END)
    return builder


def run_hunt(runtime: HuntRuntime, *, checkpointer=None, thread_id: Optional[str] = None) -> HuntOutcome:
    """Compile and run the root hunt graph to a terminal outcome."""
    builder = build_hunt_graph(runtime)
    graph = builder.compile(checkpointer=checkpointer or MemorySaver())
    config = {"configurable": {"thread_id": thread_id or runtime.run_id}}
    final = graph.invoke({}, config=config)
    report = getattr(runtime, "_report", None)
    validation = getattr(runtime, "_validation", None)
    return HuntOutcome(
        run_id=runtime.run_id,
        outcome=final.get("outcome", ""),
        report=report,
        validation=validation,
        relevant_jobs=final.get("relevant", 0),
        qualified_jobs=runtime.result.qualified_jobs,
        companies=runtime.campaign.company_count,
        obligations=runtime.campaign.obligation_count,
    )
