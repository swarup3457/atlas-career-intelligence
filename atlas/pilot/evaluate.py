"""Deterministic job validation for the pilot (audit 3.2-3.5, 3.9; prompt s.7-9, 13-14).

Turns each company's tool-collected :class:`JobDetailEvidence` into an ACCEPTED India row,
a REJECTED row, or a FOREIGN lead, using ONLY the deterministic gates: the India geography
hard gate, conjunctive lane qualification (incl. the React supported-backend gate), the
experience gate, and evidence-classed candidate matching. ``GENERAL_SOFTWARE`` never enters
the main output. Every accepted row carries the eight audit-evidence fields; foreign and
sponsored-international findings are routed to a side artifact, never the main sheet.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from typing import Optional

from atlas.candidate.search_profile import CandidateSearchProfile
from atlas.hunt.geography import GeoDecision, MAIN_OUTPUT_ALLOWED
from atlas.hunt.matching import CandidateMatchDecision, match_qualified
from atlas.hunt.models import JobDetailRevision
from atlas.hunt.pipeline import JobEvaluation, evaluate_detail
from atlas.hunt.qualification import QualificationStatus
from atlas.hunt.role_intent import RoleIntentPolicy
from atlas.hunt.stack_evidence import analyze_technology_evidence
from atlas.pilot.config import PilotConfig
from atlas.pilot.models import CompanySearchResult, JobDetailEvidence, JobRejection
from atlas.policy.loader import PolicyBundle

__all__ = ["AcceptedJob", "CompanyEvaluation", "PilotEvaluation", "evaluate_pilot"]


def _is_real_job_title(title: object) -> bool:
    """True when a captured record's title reads like a real job posting. Empty
    titles and banner/nav fragments are not jobs (shared with the agentic capture
    guard). Kept here so evaluation is a deterministic backstop for every route."""
    try:
        from atlas.pilot.agentic_tools import looks_like_job_title
        return looks_like_job_title(title)
    except Exception:  # pragma: no cover - defensive
        t = str(title or "").strip()
        return bool(t) and len(t) >= 3

_FOREIGN_LEAD_DECISIONS = frozenset(
    {GeoDecision.FOREIGN_EXCLUDED.value, GeoDecision.INTERNATIONAL_SPONSORED_LEAD.value}
)

# Requirement-evidence vocabulary (canonical -> aliases) used to extract the required
# technologies stated in an official JD so recommended rows carry requirement evidence.
_REQ_VOCAB: dict[str, tuple[str, ...]] = {
    "Java": ("java", "jvm"), "Spring Boot": ("spring boot", "spring"), "Hibernate": ("hibernate",),
    "REST APIs": ("rest apis", "restful", "rest api"), "Microservices": ("microservices",),
    "MySQL": ("mysql",), "PostgreSQL": ("postgresql", "postgres"), "SQL": ("sql",),
    "React": ("react", "reactjs"), "Angular": ("angular",), "Vue": ("vue",),
    "JavaScript": ("javascript",), "TypeScript": ("typescript",), "HTML": ("html",), "CSS": ("css",),
    "C#": ("c#",), "ASP.NET": ("asp.net", "asp .net"), ".NET": (".net", "dotnet"),
    "Payroll": ("payroll",), "HCM": ("hcm",), "HRIS": ("hris",),
    "Python": ("python",), "Node.js": ("node.js", "nodejs"), "Go": ("golang",), "Kafka": ("kafka",),
    "Maven": ("maven",), "Gradle": ("gradle",), "Docker": ("docker",), "Kubernetes": ("kubernetes",),
    "AWS": ("aws",), "Azure": ("azure",), "GCP": ("gcp",),
}


def _extract_requirements(text: str, limit: int = 14) -> tuple[str, ...]:
    low = (text or "").lower()
    out: list[str] = []
    for canonical, aliases in _REQ_VOCAB.items():
        for alias in aliases:
            if re.search(r"(?<![a-z0-9+.#])" + re.escape(alias) + r"(?![a-z0-9+#])", low):
                out.append(canonical)
                break
        if len(out) >= limit:
            break
    return tuple(out)


def _parse_date(value: str) -> Optional[datetime.date]:
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _to_revision(job: JobDetailEvidence) -> JobDetailRevision:
    mandatory = job.mandatory_requirements or _extract_requirements(job.description or job.experience_text)
    return JobDetailRevision(
        revision_id=f"pilot:{job.company}:{job.requisition_id or job.title}",
        snapshot_id=f"pilot:{job.company}", source_job_id=job.requisition_id or job.title,
        company=job.company, title=job.title, description=job.description,
        mandatory_requirements=tuple(mandatory), preferred_requirements=tuple(job.preferred_requirements),
        experience_text=job.experience_text or job.description, location=job.location,
        work_mode=(job.work_mode or "UNKNOWN"), posted_date=_parse_date(job.posted_date),
        requisition_id=job.requisition_id, official_url=job.official_url,
        verification_state="VERIFIED_OFFICIAL", has_live_official_page=True,
        evidence_texts=tuple(job.evidence_snippets),
        eligibility_text=job.eligibility_text or f"{job.location}. {(job.description or '')[:400]}",
        source_family=job.source_family or "OFFICIAL_CAREERS", record_class="OFFICIAL_DIRECT",
        discovery_channels=(job.source_family or "OFFICIAL_CAREERS",),
    )


@dataclass
class AcceptedJob:
    company: str
    title: str
    location: str
    lane: str
    role_family: str
    match: CandidateMatchDecision
    evaluation: JobEvaluation
    official_url: str
    requisition_id: str
    geography_decision: str
    location_evidence: str
    supported_stack_evidence: str
    unsupported_mandatory_backend: str
    experience_decision: str
    requirement_evidence: str
    llm_search_model: str
    company_search_task_id: str
    posted_date: str
    updated_date: str
    geography_class: str = ""   # fine-grained typed decision (INDIA_PRIMARY/SECONDARY/REMOTE_INDIA/INDIA_WIDE)

    @property
    def is_recommended(self) -> bool:
        return self.match.is_apply_family


@dataclass
class CompanyEvaluation:
    company: str
    status: str
    accepted: list[AcceptedJob] = field(default_factory=list)
    rejected: list[JobRejection] = field(default_factory=list)
    foreign_leads: list[dict] = field(default_factory=list)
    secondary_audit: list[dict] = field(default_factory=list)


@dataclass
class PilotEvaluation:
    companies: list[CompanyEvaluation] = field(default_factory=list)
    accepted: list[AcceptedJob] = field(default_factory=list)
    rejected: list[JobRejection] = field(default_factory=list)
    foreign_leads: list[dict] = field(default_factory=list)
    secondary_audit: list[dict] = field(default_factory=list)


def evaluate_company(
    result: CompanySearchResult,
    intent: RoleIntentPolicy,
    policy: PolicyBundle,
    profile: CandidateSearchProfile,
    *,
    today: Optional[datetime.date] = None,
) -> CompanyEvaluation:
    candidate = profile.to_hunt_candidate()
    ce = CompanyEvaluation(company=result.company, status=result.status)
    seen: set[str] = set()
    for job in result.jobs:
        detail = _to_revision(job)
        key = detail.canonical_key
        if key in seen:
            continue
        seen.add(key)
        # Deterministic non-job / thin-capture guard (pass-4): a captured "job"
        # with no real job title (empty, or a banner/nav fragment) must NEVER be
        # accepted, regardless of route (the ATS fast path bypasses the browser
        # capture guard). Route it to a skipped bucket so it cannot become a
        # candidate row. This is the source-side backstop the product validator
        # also enforces.
        if not _is_real_job_title(detail.title):
            ce.rejected.append(JobRejection(
                title=(detail.title or "(no title captured)"), lane="", reason_code="REJECT_NON_JOB_CAPTURE",
                detail="captured record has no usable job title (banner/thin/ATS-noise capture)",
                location=detail.location, url=detail.official_url,
            ))
            continue
        ev = evaluate_detail(
            detail, intent, policy, candidate_years=profile.total_experience_years,
            overall_evidence_strong=not profile.synthetic, today=today,
        )
        geo = ev.geo_decision
        tech = analyze_technology_evidence(
            detail.title, detail.description, detail.mandatory_requirements, detail.preferred_requirements
        )

        if ev.final_status == "REJECT_LOCATION" and geo and geo.decision in _FOREIGN_LEAD_DECISIONS:
            ce.foreign_leads.append({
                "company": result.company, "title": detail.title, "location": detail.location,
                "geography_decision": geo.decision, "url": detail.official_url,
                "reason": "; ".join(geo.reasons),
            })
            continue
        if ev.final_status != QualificationStatus.QUALIFIED.value:
            ce.rejected.append(JobRejection(
                title=detail.title, lane=(ev.final_lane or ""), reason_code=ev.final_status,
                detail="; ".join(_reasons(ev)), location=detail.location, url=detail.official_url,
            ))
            continue
        lane = ev.final_lane or ""
        if lane == "GENERAL_SOFTWARE":
            ce.secondary_audit.append({
                "company": result.company, "title": detail.title, "location": detail.location,
                "lane": lane, "url": detail.official_url, "note": "GENERAL_SOFTWARE (secondary audit only)",
            })
            continue
        # qualified, India-eligible, specific target lane -> ACCEPTED
        m = match_qualified(detail, ev.qualification.by_lane[lane], candidate, today=today, geo=geo)
        accepted = AcceptedJob(
            company=result.company, title=detail.title, location=detail.location, lane=lane,
            role_family=ev.qualification.by_lane[lane].role_family, match=m, evaluation=ev,
            official_url=detail.official_url, requisition_id=detail.requisition_id,
            # Section 13: the Geography_Decision AUDIT COLUMN must read INDIA_ELIGIBLE for an
            # accepted India row. The fine-grained typed decision (INDIA_PRIMARY/SECONDARY/
            # REMOTE_INDIA/INDIA_WIDE) is preserved in geography_class and the location evidence.
            geography_decision="INDIA_ELIGIBLE",
            geography_class=(geo.decision if geo else ""),
            location_evidence=(
                f"{geo.decision}: {geo.location_evidence} :: {'; '.join(geo.reasons)}"
                if geo else detail.location
            ),
            supported_stack_evidence=_stack_evidence(tech, ev),
            # An unsupported backend is only a DISQUALIFIER when no supported backend anchors
            # the role. For a Java/.NET-anchored role, any other language is a candidate GAP
            # (surfaced in requirement evidence / missing requirements), not a hard exclusion.
            unsupported_mandatory_backend=(
                ", ".join(tech.unsupported_mandatory_backend) if not tech.supported_backend else ""
            ),
            experience_decision=f"{m.experience_fit}",
            requirement_evidence=_requirement_evidence(m),
            llm_search_model=result.model, company_search_task_id=result.task_id,
            posted_date=job.posted_date, updated_date=job.updated_date,
        )
        ce.accepted.append(accepted)
    return ce


def _reasons(ev: JobEvaluation) -> list[str]:
    dec = ev.qualification.primary or next(iter(ev.qualification.by_lane.values()), None)
    return list(dec.reasons) if dec else []


def _stack_evidence(tech, ev: JobEvaluation) -> str:
    parts = []
    if tech.supported_stack_summary:
        parts.append(tech.supported_stack_summary)
    lane = ev.final_lane
    if lane and lane in ev.qualification.by_lane:
        anchors = ev.qualification.by_lane[lane].matched_anchors
        if anchors:
            parts.append("anchors: " + ", ".join(anchors))
    return " | ".join(parts)


def _requirement_evidence(m: CandidateMatchDecision) -> str:
    matched = ", ".join(m.requirements_matched)
    missing = ", ".join(m.missing_requirements)
    out = []
    if matched:
        out.append(f"matched: {matched}")
    if missing:
        out.append(f"missing: {missing}")
    return " | ".join(out)


def evaluate_pilot(
    results: list[CompanySearchResult],
    intent: RoleIntentPolicy,
    policy: PolicyBundle,
    profile: CandidateSearchProfile,
    *,
    today: Optional[datetime.date] = None,
) -> PilotEvaluation:
    pe = PilotEvaluation()
    for result in results:
        ce = evaluate_company(result, intent, policy, profile, today=today)
        pe.companies.append(ce)
        pe.accepted.extend(ce.accepted)
        pe.rejected.extend(ce.rejected)
        pe.foreign_leads.extend(ce.foreign_leads)
        pe.secondary_audit.extend(ce.secondary_audit)
    return pe
