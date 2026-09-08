"""Two-tier candidate ranking + deep evaluation (Phase 1E/F §8.B/§8.C).

Inspired by the reviewed MIT research reference's `/rank` command (reimplemented
Atlas-native, no code copied; exact repo + license recorded in
docs/EXTERNAL_RESEARCH_DECISIONS.md): jobs are SELECTED with a query (never the
whole backlog is loaded into a reasoning worker), triaged in small batches, and
only the top/selected jobs get a deep fit review. Nothing here is a completion authority; the deterministic
eligibility gate is the ceiling an optional LLM can reorder under but never
override. Every score axis is persisted separately — this is a candidate-fit
score, never an "ATS score".
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from atlas.candidate.eligibility import (
    CandidateProfile,
    EligibilityAxes,
    EligibilityGate,
    EligibilityStatus,
    RankableJob,
    Recommendation,
    match_requirement,
)
from atlas.candidate.models import EvidenceClass, _STRENGTH
from atlas.policy.loader import PolicyBundle

REASONING_VERSION = "1EF.rank.1"

# Freshness urgency contribution to the triage score.
_FRESHNESS_URGENCY = {
    "0-7 days": 20, "8-14 days": 12, "15-30 days": 6,
    "31-45 days exceptional": 2, "LIVE_DATE_UNKNOWN": 4,
    "DATE_UNKNOWN": 0, "DATA_CONFLICT": 0, "STALE": -10,
}


@dataclass(frozen=True)
class SelectionQuery:
    """A bounded selection query — the way candidates are chosen for a reasoning
    pass. NEVER load the whole backlog into a worker."""

    lane: Optional[str] = None
    location_group: str = "PRIMARY"
    limit: int = 50


@dataclass(frozen=True)
class JobEvaluation:
    job_key: str
    company: str
    title: str
    lane: Optional[str]
    eligibility: str
    verification: str
    freshness: str
    candidate_fit: Optional[int]   # 0-100, or None when not scored (dead/unfetchable)
    recommendation: str
    confidence: float
    reasoning_model: str
    reasoning_version: str
    supported_evidence_ids: tuple[str, ...] = ()
    strengths: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    tier: str = "TRIAGE"           # TRIAGE | DEEP | NOT_EVALUATED
    selected: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_key": self.job_key, "company": self.company, "title": self.title,
            "lane": self.lane, "eligibility": self.eligibility, "verification": self.verification,
            "freshness": self.freshness, "candidate_fit": self.candidate_fit,
            "recommendation": self.recommendation, "confidence": round(self.confidence, 3),
            "reasoning_model": self.reasoning_model, "reasoning_version": self.reasoning_version,
            "supported_evidence_ids": list(self.supported_evidence_ids),
            "strengths": list(self.strengths), "gaps": list(self.gaps),
            "tier": self.tier, "selected": self.selected,
        }


@dataclass(frozen=True)
class RankingResult:
    evaluations: tuple[JobEvaluation, ...]
    eligible: tuple[str, ...]
    selected: tuple[str, ...]
    deferred: tuple[str, ...]
    excluded: tuple[str, ...]
    not_evaluated: tuple[str, ...]
    controller_usage: Mapping[str, Any] = field(default_factory=dict)

    def by_key(self, job_key: str) -> Optional[JobEvaluation]:
        for e in self.evaluations:
            if e.job_key == job_key:
                return e
        return None


def _ratio(reqs: Sequence[str], evidence: Mapping[str, EvidenceClass]) -> tuple[float, list[str], list[str], list[str]]:
    """Return (supported_ratio, supported_reqs, missing_reqs, matched_topics)."""
    if not reqs:
        return 1.0, [], [], []
    supported: list[str] = []
    missing: list[str] = []
    matched_topics: list[str] = []
    for req in reqs:
        topic = match_requirement(req, evidence)
        if topic is not None:
            supported.append(req)
            matched_topics.append(topic)
        else:
            missing.append(req)
    return len(supported) / len(reqs), supported, missing, matched_topics


class TriageRanker:
    """Tier 1: cheap deterministic triage over a SELECTED, bounded set of jobs.

    Ordering and scores are a pure function of the inputs, so the result is
    identical for any worker count (concurrency 1 == N). An optional controller
    is consulted in ~5-job batches for advisory strengths only and can never
    change a deterministic veto; quarantined output is ignored."""

    def __init__(
        self,
        policy: PolicyBundle,
        candidate: CandidateProfile,
        *,
        controller: Optional[Any] = None,
        model: str = "none",
        triage_limit: int = 25,
        batch_size: int = 5,
    ) -> None:
        self.policy = policy
        self.candidate = candidate
        self.gate = EligibilityGate(policy)
        self.controller = controller
        self.model = model if controller is not None else "none"
        self.triage_limit = triage_limit
        self.batch_size = max(1, batch_size)

    def select(self, jobs: Sequence[RankableJob], query: SelectionQuery) -> list[RankableJob]:
        """Bounded, query-scoped selection (never the whole backlog)."""
        out: list[RankableJob] = []
        for job in jobs:
            axes = self.gate.evaluate(job, self.candidate)
            if query.lane and axes.lane != query.lane:
                continue
            out.append(job)
            if len(out) >= query.limit:
                break
        return out

    def _triage_score(self, job: RankableJob, axes: EligibilityAxes) -> int:
        mand_ratio, *_ = _ratio(job.mandatory_requirements, self.candidate.evidence)
        pref_ratio, *_ = _ratio(job.preferred_requirements, self.candidate.evidence)
        score = mand_ratio * 60 + pref_ratio * 20
        score += _FRESHNESS_URGENCY.get(axes.freshness, 0)
        return max(0, min(100, round(score)))

    def rank(self, jobs: Sequence[RankableJob], *, today: Optional[datetime.date] = None) -> RankingResult:
        evals: dict[str, JobEvaluation] = {}
        eligible: list[tuple[int, str, RankableJob, EligibilityAxes]] = []
        deferred: list[str] = []
        excluded: list[str] = []

        for job in jobs:
            axes = self.gate.evaluate(job, self.candidate, today=today)
            if not job.is_fetchable:
                # dead/unfetchable posting: never scored from the title alone
                evals[job.job_key] = JobEvaluation(
                    job_key=job.job_key, company=job.company, title=job.title, lane=axes.lane,
                    eligibility=axes.status, verification=axes.verification_state, freshness=axes.freshness,
                    candidate_fit=None, recommendation=Recommendation.MANUAL_VERIFICATION.value,
                    confidence=0.0, reasoning_model=self.model, reasoning_version=REASONING_VERSION,
                    tier="NOT_EVALUATED", selected=False,
                )
                continue
            if axes.status in (EligibilityStatus.EXCLUDED.value, EligibilityStatus.NOT_ELIGIBLE.value,
                               EligibilityStatus.CLOSED.value):
                rec = Recommendation.CLOSED if axes.status == EligibilityStatus.CLOSED.value else Recommendation.REJECT
                evals[job.job_key] = JobEvaluation(
                    job_key=job.job_key, company=job.company, title=job.title, lane=axes.lane,
                    eligibility=axes.status, verification=axes.verification_state, freshness=axes.freshness,
                    candidate_fit=0, recommendation=rec.value, confidence=0.9,
                    reasoning_model=self.model, reasoning_version=REASONING_VERSION,
                    gaps=axes.vetoes, tier="TRIAGE", selected=False,
                )
                excluded.append(job.job_key)
                continue
            score = self._triage_score(job, axes)
            eligible.append((score, job.job_key, job, axes))
            if axes.status == EligibilityStatus.DEFERRED.value:
                deferred.append(job.job_key)

        # deterministic stable order: score desc, then job_key asc
        eligible.sort(key=lambda x: (-x[0], x[1]))

        # optional advisory controller consult in ~batch_size batches (never
        # changes deterministic scores/vetoes; only records usage + advisory notes)
        advisory = self._consult_controller([j for _, _, j, _ in eligible])

        eligible_keys: list[str] = []
        for rank_index, (score, key, job, axes) in enumerate(eligible):
            eligible_keys.append(key)
            within = rank_index < self.triage_limit
            mand_ratio, supported, missing, topics = _ratio(job.mandatory_requirements, self.candidate.evidence)
            supported_ids = tuple(
                self.candidate.evidence_ids[t] for t in topics if t in self.candidate.evidence_ids
            )
            strengths = tuple(supported) + tuple(advisory.get(key, ()))
            evals[key] = JobEvaluation(
                job_key=key, company=job.company, title=job.title, lane=axes.lane,
                eligibility=axes.status, verification=axes.verification_state, freshness=axes.freshness,
                candidate_fit=score if within else None,
                recommendation=(Recommendation.MONITOR.value if within else Recommendation.NOT_EVALUATED.value),
                confidence=0.5 if within else 0.0,
                reasoning_model=self.model, reasoning_version=REASONING_VERSION,
                supported_evidence_ids=supported_ids, strengths=strengths, gaps=tuple(missing),
                tier="TRIAGE" if within else "NOT_EVALUATED", selected=False,
            )

        ordered = [k for _, k, _, _ in eligible]
        selected = tuple(ordered[: self.triage_limit])
        not_evaluated = tuple(
            k for k, e in evals.items() if e.tier == "NOT_EVALUATED"
        )
        return RankingResult(
            evaluations=tuple(evals.values()),
            eligible=tuple(eligible_keys),
            selected=selected,
            deferred=tuple(deferred),
            excluded=tuple(excluded),
            not_evaluated=not_evaluated,
            controller_usage=(self.controller.usage() if self.controller is not None
                              and hasattr(self.controller, "usage") else {}),
        )

    def _consult_controller(self, eligible_jobs: Sequence[RankableJob]) -> dict[str, tuple[str, ...]]:
        """Optional, advisory-only. Batches jobs ~batch_size and asks the
        triage-ranker agent for extra strengths. Never alters deterministic
        scores/vetoes; quarantined/invalid output is ignored."""
        if self.controller is None or not hasattr(self.controller, "run_agent"):
            return {}
        import json

        advisory: dict[str, tuple[str, ...]] = {}
        for i in range(0, len(eligible_jobs), self.batch_size):
            batch = eligible_jobs[i: i + self.batch_size]
            payload = {
                "jobs": [
                    {"job_key": j.job_key, "title": j.title,
                     "mandatory": list(j.mandatory_requirements)} for j in batch
                ],
                "candidate_evidence_tokens": sorted(self.candidate.evidence.keys()),
            }
            try:
                result = self.controller.run_agent("triage-ranker", "rank batch", payload)
            except Exception:  # noqa: BLE001 - advisory only, never fatal
                continue
            if result.quarantined or not result.content:
                continue
            try:
                parsed = json.loads(result.content)
                for row in parsed.get("scores", []) or []:
                    key = str(row.get("job_key", ""))
                    notes = tuple(str(s) for s in (row.get("strengths", []) or []))
                    if key:
                        advisory[key] = notes
            except (ValueError, TypeError):
                continue
        return advisory


class DeepEvaluator:
    """Tier 2: deep fit review for ONLY the top/selected jobs."""

    def __init__(
        self,
        policy: PolicyBundle,
        candidate: CandidateProfile,
        *,
        model: str = "none",
        deep_limit: int = 10,
    ) -> None:
        self.policy = policy
        self.candidate = candidate
        self.gate = EligibilityGate(policy)
        self.model = model
        self.deep_limit = deep_limit

    def _confidence(self, matched_topics: Sequence[str]) -> float:
        if not matched_topics:
            return 0.2
        strong = sum(
            1 for t in matched_topics
            if self.candidate.evidence.get(t) in (EvidenceClass.PROFESSIONAL, EvidenceClass.PROJECT_PRODUCT)
        )
        return round(0.4 + 0.6 * (strong / len(matched_topics)), 3)

    def evaluate(
        self, job: RankableJob, *, today: Optional[datetime.date] = None
    ) -> JobEvaluation:
        axes = self.gate.evaluate(job, self.candidate, today=today)
        mand_ratio, m_sup, m_miss, m_topics = _ratio(job.mandatory_requirements, self.candidate.evidence)
        pref_ratio, p_sup, p_miss, p_topics = _ratio(job.preferred_requirements, self.candidate.evidence)
        matched = m_topics + p_topics
        supported_ids = tuple(
            self.candidate.evidence_ids[t] for t in matched if t in self.candidate.evidence_ids
        )
        fit = round(mand_ratio * 70 + pref_ratio * 30)

        # recommendation mapping (deterministic; eligibility gate is the ceiling)
        if axes.status == EligibilityStatus.CLOSED.value:
            rec = Recommendation.CLOSED
        elif axes.status in (EligibilityStatus.EXCLUDED.value, EligibilityStatus.NOT_ELIGIBLE.value):
            rec = Recommendation.REJECT
        elif axes.verification_state in ("PORTAL_CURRENT_LEAD", "MANUAL_VERIFICATION"):
            rec = Recommendation.MANUAL_VERIFICATION
        elif mand_ratio >= 1.0:
            if axes.verification_state == "VERIFIED_OFFICIAL" and axes.freshness == "0-7 days":
                rec = Recommendation.PRIORITY_APPLY
            else:
                rec = Recommendation.STRONG_APPLY
        elif mand_ratio >= 0.5:
            rec = Recommendation.APPLY_AFTER_TAILORING
        else:
            rec = Recommendation.STRETCH

        return JobEvaluation(
            job_key=job.job_key, company=job.company, title=job.title, lane=axes.lane,
            eligibility=axes.status, verification=axes.verification_state, freshness=axes.freshness,
            candidate_fit=fit, recommendation=rec.value, confidence=self._confidence(matched),
            reasoning_model=self.model, reasoning_version=REASONING_VERSION,
            supported_evidence_ids=supported_ids, strengths=tuple(m_sup + p_sup), gaps=tuple(m_miss),
            tier="DEEP", selected=True,
        )


def rank_and_evaluate(
    jobs: Sequence[RankableJob],
    policy: PolicyBundle,
    candidate: CandidateProfile,
    *,
    controller: Optional[Any] = None,
    model: str = "none",
    triage_limit: int = 25,
    deep_limit: int = 10,
    today: Optional[datetime.date] = None,
) -> RankingResult:
    """Convenience: triage, then deep-evaluate the top ``deep_limit`` selected
    jobs, merging deep evaluations back over their triage rows."""
    triage = TriageRanker(policy, candidate, controller=controller, model=model, triage_limit=triage_limit)
    result = triage.rank(jobs, today=today)
    deep = DeepEvaluator(policy, candidate, model=model, deep_limit=deep_limit)
    by_key = {j.job_key: j for j in jobs}
    selected_keys = list(result.selected)[:deep_limit]
    merged: dict[str, JobEvaluation] = {e.job_key: e for e in result.evaluations}
    for key in selected_keys:
        job = by_key.get(key)
        if job is None:
            continue
        merged[key] = deep.evaluate(job, today=today)
    return RankingResult(
        evaluations=tuple(merged.values()),
        eligible=result.eligible,
        selected=tuple(selected_keys),
        deferred=result.deferred,
        excluded=result.excluded,
        not_evaluated=result.not_evaluated,
        controller_usage=result.controller_usage,
    )


__all__ = [
    "REASONING_VERSION",
    "SelectionQuery",
    "JobEvaluation",
    "RankingResult",
    "TriageRanker",
    "DeepEvaluator",
    "rank_and_evaluate",
]
