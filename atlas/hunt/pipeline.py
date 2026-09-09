"""Hunt pipeline orchestration (architecture s.3, build spec 12).

Ties the stages together over a ``BoardProvider`` abstraction so the SAME
deterministic logic runs on fixtures (offline tests, ``hunt plan``) and on live
official sources. Board collection is separated from candidate policy: a
policy-only rerun reuses stored snapshots/details with ZERO network
(:func:`requalify_details`).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Mapping, Optional, Protocol, Sequence

from atlas.hunt.campaign import CompanyCampaign, CompanyRef
from atlas.hunt.experience_v2 import ExperienceFitBand
from atlas.hunt.matching import CandidateMatchDecision, HuntCandidate, match_qualified
from atlas.hunt.models import (
    BoardSnapshot,
    CoverageDecision,
    JobDetailRevision,
    LanePrefilterDecision,
    SourceCoverage,
)
from atlas.hunt.prefilter import hydration_union, prefilter_snapshot
from atlas.hunt.qualification import (
    JobQualification,
    QualifiableJob,
    QualificationStatus,
    RoleQualificationDecision,
    qualify_job,
)
from atlas.hunt.role_intent import RoleIntentPolicy
from atlas.policy.loader import PolicyBundle
from atlas.policy.rules import NOT_ELIGIBLE, freshness_band, international_eligibility

__all__ = [
    "BoardProvider",
    "FixtureProvider",
    "JobEvaluation",
    "HuntPipelineResult",
    "run_campaign",
    "requalify_details",
    "evaluate_detail",
]

_STALE_BANDS = frozenset({"STALE"})


class BoardProvider(Protocol):
    """Supplies immutable board snapshots and hydrated details. A live provider
    wraps the official ATS/careers adapters; the fixture provider serves stored
    data for offline runs and policy-only reruns."""

    def fetch_board(self, campaign_id: str, company: CompanyRef) -> BoardSnapshot: ...

    def hydrate(self, snapshot: BoardSnapshot, source_job_id: str) -> Optional[JobDetailRevision]: ...


@dataclass
class FixtureProvider:
    """Deterministic offline provider. ``boards`` maps company name -> snapshot;
    ``details`` maps source_job_id -> detail. Records a network-call counter so
    tests can prove a policy-only rerun makes zero calls."""

    boards: Mapping[str, BoardSnapshot]
    details: Mapping[str, JobDetailRevision]
    network_calls: int = 0

    def fetch_board(self, campaign_id: str, company: CompanyRef) -> BoardSnapshot:
        self.network_calls += 1
        snap = self.boards.get(company.name)
        if snap is not None:
            return snap
        return BoardSnapshot(
            snapshot_id=f"{campaign_id}:{company.name}:empty", campaign_id=campaign_id,
            company_id=company.name, company_name=company.name,
            source_instance_id=f"{company.name}:careers", source_family="OFFICIAL_CAREERS",
            route_family="OFFICIAL_CAREERS", raw_jobs=(), snapshot_status="COMPLETE",
        )

    def hydrate(self, snapshot: BoardSnapshot, source_job_id: str) -> Optional[JobDetailRevision]:
        self.network_calls += 1
        return self.details.get(source_job_id)


@dataclass(frozen=True)
class JobEvaluation:
    detail: JobDetailRevision
    qualification: JobQualification
    final_status: str            # QUALIFIED | REJECT_* | NEEDS_DETAIL | AMBIGUOUS_REVIEW
    final_lane: Optional[str]
    geo_class: str = ""
    freshness: str = ""


@dataclass
class HuntPipelineResult:
    campaign_id: str
    lanes: tuple[str, ...]
    snapshots: list[BoardSnapshot] = field(default_factory=list)
    details: list[JobDetailRevision] = field(default_factory=list)
    evaluations: list[JobEvaluation] = field(default_factory=list)
    coverage: list[CoverageDecision] = field(default_factory=list)
    source_coverage: list[SourceCoverage] = field(default_factory=list)
    matches: list[CandidateMatchDecision] = field(default_factory=list)
    network_calls: int = 0

    @property
    def raw_jobs(self) -> int:
        return sum(s.raw_job_count for s in self.snapshots)

    @property
    def hydrated_jobs(self) -> int:
        return len(self.details)

    @property
    def qualified_jobs(self) -> int:
        return sum(1 for e in self.evaluations if e.final_status == QualificationStatus.QUALIFIED.value)

    @property
    def relevant_jobs(self) -> int:
        return len(self.matches)


def evaluate_detail(
    detail: JobDetailRevision,
    intent: RoleIntentPolicy,
    policy: PolicyBundle,
    *,
    candidate_years: Optional[float] = None,
    overall_evidence_strong: bool = True,
    today: Optional[datetime.date] = None,
) -> JobEvaluation:
    """Strict qualification of one hydrated job: role/stack conjunction, then
    experience, geography, and freshness gates (reusing the existing
    deterministic rules)."""
    job = QualifiableJob(
        job_key=detail.canonical_key, title=detail.title, description=detail.description,
        mandatory_requirements=detail.mandatory_requirements,
        preferred_requirements=detail.preferred_requirements,
        experience_text=detail.experience_text, has_detail=True,
    )
    qual = qualify_job(
        job, intent, experience_policy=policy.experience,
        candidate_years=candidate_years, overall_evidence_strong=overall_evidence_strong,
    )
    geo = international_eligibility(detail.eligibility_text or detail.description, policy.geography)
    fresh = freshness_band(detail.posted_date, today=today, has_live_official_page=detail.has_live_official_page)

    if not qual.qualified:
        # surface the primary lane's (or best) rejection reason
        best = _best_reject(qual)
        return JobEvaluation(detail, qual, best.status if best else QualificationStatus.NEEDS_DETAIL.value,
                             None, geo_class=geo, freshness=fresh)

    if geo == NOT_ELIGIBLE:
        return JobEvaluation(detail, qual, "REJECT_LOCATION", qual.primary_lane, geo_class=geo, freshness=fresh)
    if fresh in _STALE_BANDS:
        return JobEvaluation(detail, qual, "REJECT_FRESHNESS", qual.primary_lane, geo_class=geo, freshness=fresh)

    return JobEvaluation(detail, qual, QualificationStatus.QUALIFIED.value, qual.primary_lane,
                         geo_class=geo, freshness=fresh)


def _best_reject(qual: JobQualification) -> Optional[RoleQualificationDecision]:
    order = [
        QualificationStatus.REJECT_EXPERIENCE.value,
        QualificationStatus.REJECT_WRONG_STACK.value,
        QualificationStatus.REJECT_ROLE_FAMILY.value,
        QualificationStatus.NEEDS_DETAIL.value,
        QualificationStatus.AMBIGUOUS_REVIEW.value,
    ]
    decisions = list(qual.by_lane.values())
    for status in order:
        for d in decisions:
            if d.status == status:
                return d
    return decisions[0] if decisions else None


def _coverage_rows(
    company: CompanyRef,
    snapshot: BoardSnapshot,
    lanes: Sequence[str],
    prefilter: Sequence[LanePrefilterDecision],
    evaluations: Sequence[JobEvaluation],
    *,
    checked_at: str,
) -> list[CoverageDecision]:
    rows: list[CoverageDecision] = []
    for lane in lanes:
        pref_ct = sum(1 for d in prefilter if d.lane == lane)
        hydrated_ct = 0
        qualified_ct = 0
        wrong_stack = role_family = experience = location = freshness = manual = 0
        for ev in evaluations:
            dec = ev.qualification.by_lane.get(lane)
            if dec is None:
                continue
            hydrated_ct += 1
            if ev.final_lane == lane and ev.final_status == QualificationStatus.QUALIFIED.value:
                qualified_ct += 1
            elif ev.final_lane == lane and ev.final_status == "REJECT_LOCATION":
                location += 1
            elif ev.final_lane == lane and ev.final_status == "REJECT_FRESHNESS":
                freshness += 1
            if dec.status == QualificationStatus.REJECT_WRONG_STACK.value:
                wrong_stack += 1
            elif dec.status == QualificationStatus.REJECT_ROLE_FAMILY.value:
                role_family += 1
            elif dec.status == QualificationStatus.REJECT_EXPERIENCE.value:
                experience += 1
            elif dec.status in (QualificationStatus.NEEDS_DETAIL.value, QualificationStatus.AMBIGUOUS_REVIEW.value):
                manual += 1
        terminal = "CHECKED" if snapshot.raw_job_count else "ZERO"
        if snapshot.access_status not in ("OK", ""):
            terminal = "ACCESS_LIMITED"
        rows.append(
            CoverageDecision(
                campaign_id=snapshot.campaign_id, company=company.name, tier=company.tier,
                group=company.group, official_domain=snapshot.source_url, source=snapshot.source_family,
                route=snapshot.route_family, check_type="DEEP", lane=lane, snapshot_id=snapshot.snapshot_id,
                pages=snapshot.pages, raw_jobs=snapshot.raw_job_count, prefiltered_jobs=pref_ct,
                hydrated_jobs=hydrated_ct, qualified_jobs=qualified_ct, wrong_stack_rejected=wrong_stack,
                role_family_rejected=role_family, experience_rejected=experience, location_rejected=location,
                freshness_rejected=freshness, manual_verification=manual, access_status=snapshot.access_status,
                terminal_status=terminal, checked_at=checked_at,
            )
        )
    return rows


def run_campaign(
    campaign: CompanyCampaign,
    intent: RoleIntentPolicy,
    policy: PolicyBundle,
    provider: BoardProvider,
    candidate: HuntCandidate,
    *,
    reuse_snapshots: Optional[Mapping[str, BoardSnapshot]] = None,
    reuse_details: Optional[Mapping[str, list[JobDetailRevision]]] = None,
    candidate_years: Optional[float] = None,
    today: Optional[datetime.date] = None,
) -> HuntPipelineResult:
    """Execute the full pipeline over every company x lane obligation."""
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    result = HuntPipelineResult(campaign_id=campaign.campaign_id, lanes=campaign.lanes)
    source_seen: dict[str, SourceCoverage] = {}

    for company in campaign.companies:
        snapshot = (reuse_snapshots or {}).get(company.name) or provider.fetch_board(campaign.campaign_id, company)
        result.snapshots.append(snapshot)

        prefilter = prefilter_snapshot(snapshot, intent)
        if reuse_details is not None:
            details = list(reuse_details.get(company.name, []))
        else:
            union = hydration_union(prefilter)
            details = []
            for sid in union:
                d = provider.hydrate(snapshot, sid)
                if d is not None:
                    details.append(d)
        result.details.extend(details)

        evaluations = [
            evaluate_detail(d, intent, policy, candidate_years=candidate_years,
                            overall_evidence_strong=candidate.strong_overall, today=today)
            for d in details
        ]
        result.evaluations.extend(evaluations)

        for ev in evaluations:
            if ev.final_status == QualificationStatus.QUALIFIED.value and ev.final_lane:
                result.matches.append(
                    match_qualified(ev.detail, ev.qualification.by_lane[ev.final_lane], candidate, today=today)
                )

        result.coverage.extend(
            _coverage_rows(company, snapshot, campaign.lanes, prefilter, evaluations, checked_at=now_iso)
        )

        sc = source_seen.get(snapshot.source_instance_id)
        if sc is None:
            sc = SourceCoverage(
                source_instance_id=snapshot.source_instance_id, source_family=snapshot.source_family,
                route=snapshot.route_family, health=snapshot.source_health, adapter_version=snapshot.adapter_version,
                parser_version=snapshot.parser_version, limitation=snapshot.access_status if snapshot.access_status != "OK" else "",
            )
            source_seen[snapshot.source_instance_id] = sc
        sc.pages += snapshot.pages
        sc.request_count += 1
        sc.raw_jobs += snapshot.raw_job_count
        sc.companies += 1
        sc.qualified_jobs += sum(
            1 for ev in evaluations if ev.final_status == QualificationStatus.QUALIFIED.value
        )

    result.source_coverage = list(source_seen.values())
    result.network_calls = getattr(provider, "network_calls", 0)
    return result


def requalify_details(
    campaign: CompanyCampaign,
    intent: RoleIntentPolicy,
    policy: PolicyBundle,
    candidate: HuntCandidate,
    stored_snapshots: Mapping[str, BoardSnapshot],
    stored_details: Mapping[str, list[JobDetailRevision]],
    *,
    candidate_years: Optional[float] = None,
    today: Optional[datetime.date] = None,
) -> HuntPipelineResult:
    """Policy-only rerun: requalify stored snapshots/details with a new role
    policy and ZERO network calls (build spec 12.5). The provider is never
    consulted."""

    class _NoNetwork:
        network_calls = 0

        def fetch_board(self, campaign_id, company):  # pragma: no cover - must not be called
            raise AssertionError("requalify_details must not fetch boards")

        def hydrate(self, snapshot, source_job_id):  # pragma: no cover - must not be called
            raise AssertionError("requalify_details must not hydrate")

    return run_campaign(
        campaign, intent, policy, _NoNetwork(), candidate,
        reuse_snapshots=stored_snapshots, reuse_details=stored_details,
        candidate_years=candidate_years, today=today,
    )
