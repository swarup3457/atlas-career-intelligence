"""The single report trust boundary (PRODUCTION R1 §4).

A worker only ever *proposes* a job. A proposal may become a ``Validated_Jobs``
row only after it independently passes the non-bypassable Python decision in
:func:`atlas.browser_backend.validation.validate_job_evidence` (HTTPS official /
ATS URL, real title, India-only geography, full-detail evidence, grounded quotes,
not closed, experience gate, role/stack anchors).

Every report builder MUST route proposals through :func:`partition_jobs` instead
of trusting ``proposed_decision`` and hardcoding ``PYTHON_VALIDATED`` /
``MANUAL_REVIEW``. Failed proposals never reach ``Validated_Jobs`` — they are
returned as :class:`RejectedJob` for the ``Rejected_Jobs`` sheet, tagged with the
actual Python rejection reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

from atlas.browser_backend.validation import validate_job_evidence
from atlas.pilot.models import JobDetailEvidence
from atlas.pilot.normalize import normalize_source_text
from atlas.policy.rules import extract_experience

# Worker verdicts that mean "I propose this as an accepted India job". Only these
# are candidates for Validated_Jobs; everything else is skipped here (a worker's
# own rejections / foreign leads keep flowing through their dedicated lists).
_ACCEPT_PROPOSALS = {"accept", "accepted", "validate", "propose"}

VALIDATED_STATUS = "PYTHON_VALIDATED"


@dataclass(frozen=True)
class ValidatedJob:
    """An accepted job that passed the independent Python decision."""

    evidence: JobDetailEvidence
    lane: str
    recommendation: str
    source: dict[str, Any]
    verification_status: str = VALIDATED_STATUS


@dataclass(frozen=True)
class RejectedJob:
    """A worker acceptance proposal that the Python decision rejected."""

    title: str
    location: str
    url: str
    lane: str
    reason_code: str
    detail: str


def recommendation_for(evidence: JobDetailEvidence, lane: str) -> str:
    """A real Python recommendation for a VALIDATED job (never hardcoded).

    The job already cleared the hard experience gate, so the tier only separates
    an immediate apply from a light-tailoring apply from a genuine stretch.
    """
    parts = [evidence.experience_text or "", evidence.description or ""]
    parts.extend(str(x) for x in (evidence.mandatory_requirements or ()))
    exp = extract_experience(normalize_source_text(" \n ".join(p for p in parts if p)))
    min_years = getattr(exp, "min_years", None)
    if min_years is None or min_years < 3:
        return "APPLY_NOW"
    if min_years < 4:
        return "APPLY_AFTER_TAILORING"
    return "STRETCH"


def classify_job(
    job: dict[str, Any],
    *,
    official_domain: str,
    company: str,
    candidate_max_years: Optional[float] = None,
) -> tuple[Optional[ValidatedJob], Optional[RejectedJob]]:
    """Independently decide one proposal. Returns (validated, None) or (None, rejected)."""
    proposal = dict(job)
    proposal.setdefault("official_url", proposal.get("canonical_url", proposal.get("url", "")))
    proposal.setdefault("description", proposal.get("detail_text", proposal.get("description", "")))
    proposal.setdefault(
        "evidence_snippets",
        proposal.get("evidence_snippets", proposal.get("evidence_quotes", [])),
    )
    company_name = company or str(job.get("company", ""))
    lane = str(job.get("lane", "")).strip().upper()
    validation = validate_job_evidence(
        proposal, candidate_max_years=candidate_max_years,
        official_domain=official_domain, company=company_name,
    )
    if validation.accepted is not None:
        return ValidatedJob(
            evidence=validation.accepted, lane=lane,
            recommendation=recommendation_for(validation.accepted, lane), source=dict(job),
        ), None
    rejection = validation.rejection
    return None, RejectedJob(
        title=rejection.title, location=rejection.location, url=rejection.url,
        lane=rejection.lane or lane, reason_code=rejection.reason_code, detail=rejection.detail,
    )


def partition_jobs(
    jobs: Iterable[dict[str, Any]] | None,
    *,
    official_domain: str,
    company: str,
    candidate_max_years: Optional[float] = None,
) -> tuple[list[ValidatedJob], list[RejectedJob]]:
    """Split worker acceptance proposals into (validated, rejected) by the Python decision."""
    validated: list[ValidatedJob] = []
    rejected: list[RejectedJob] = []
    for job in jobs or []:
        if not isinstance(job, dict):
            continue
        if str(job.get("proposed_decision", "accept")).strip().lower() not in _ACCEPT_PROPOSALS:
            continue
        accepted, rej = classify_job(
            job, official_domain=official_domain, company=company,
            candidate_max_years=candidate_max_years,
        )
        if accepted is not None:
            validated.append(accepted)
        elif rej is not None:
            rejected.append(rej)
    return validated, rejected
