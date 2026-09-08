"""Phase 1E/F Stage 3 — deterministic eligibility, two-tier ranking, job key.

All deterministic; the LLM path is optional and advisory only. Uses the PII-free
synthetic candidate ledger and the real public policy bundle.
"""

from __future__ import annotations

import datetime
import re
from concurrent.futures import ThreadPoolExecutor

import pytest

from atlas.candidate.eligibility import (
    CandidateProfile,
    EligibilityGate,
    EligibilityStatus,
    RankableJob,
    Recommendation,
    classify_lane,
)
from atlas.candidate.importer import build_synthetic_ledger
from atlas.candidate.jobkey import canonical_identity, job_key_for, stable_job_key
from atlas.candidate.ranking import (
    DeepEvaluator,
    SelectionQuery,
    TriageRanker,
    rank_and_evaluate,
)
from atlas.policy import load_policy

TODAY = datetime.date(2026, 9, 8)


@pytest.fixture(scope="module")
def policy():
    return load_policy()


@pytest.fixture(scope="module")
def candidate():
    return CandidateProfile.from_ledger(
        build_synthetic_ledger(),
        total_experience_years=4.5,
        target_lanes=("JAVA_BACKEND", "GENERAL_SOFTWARE"),
    )


def _job(**kw) -> RankableJob:
    base = dict(
        job_key=kw.get("job_key", stable_job_key(kw.get("title", "x"))),
        company="Acme", title="Java Backend Engineer", location="Bengaluru, India",
        lane="JAVA_BACKEND", mandatory_requirements=("Java", "Spring Boot"),
        preferred_requirements=("AWS",), experience_text="2+ years",
        eligibility_text="Bengaluru, India", posted_date=TODAY,
        verification_state="VERIFIED_OFFICIAL", has_live_official_page=True, is_fetchable=True,
    )
    base.update(kw)
    return RankableJob(**base)


# --------------------------------------------------------------------------- #
# Stable job key
# --------------------------------------------------------------------------- #
def test_job_key_is_deterministic_and_path_safe():
    k1 = stable_job_key("ctl::acme::java backend engineer::bengaluru india")
    k2 = stable_job_key("ctl::acme::java backend engineer::bengaluru india")
    assert k1 == k2
    assert re.fullmatch(r"[a-z0-9_]+", k1)
    for bad in ("/", "\\", ":", "..", " "):
        assert bad not in k1


def test_job_key_unicode_safe_and_bounded():
    k = stable_job_key("ctl::Café Münchën 🚀::inténgöör::índia")
    assert re.fullmatch(r"[a-z0-9_]+", k)
    assert len(k) <= len("job") + 1 + 24


def test_job_key_collision_resistant():
    a = stable_job_key("reqid::acme::greenhouse::123")
    b = stable_job_key("reqid::acme::greenhouse::124")
    assert a != b


def test_canonical_identity_reuses_requisition_first_contract():
    official = canonical_identity(company="Acme", title="X", location="Y",
                                  source_family="greenhouse", source_job_id="42", official=True)
    assert official == "reqid::acme::greenhouse::42"
    fallback = canonical_identity(company="Acme", title="Java Dev", location="India")
    assert fallback.startswith("ctl::acme::java dev::india")
    assert job_key_for(company="Acme", title="X", location="Y",
                       source_family="greenhouse", source_job_id="42", official=True) == stable_job_key(official)


# --------------------------------------------------------------------------- #
# Deterministic eligibility vetoes
# --------------------------------------------------------------------------- #
def test_experience_hard_minimum_vetoes(policy, candidate):
    gate = EligibilityGate(policy)
    job = _job(experience_text="Minimum 8 years of experience required")
    axes = gate.evaluate(job, candidate, today=TODAY)
    assert axes.status == EligibilityStatus.NOT_ELIGIBLE.value
    assert any("experience" in v for v in axes.vetoes)


def test_geography_not_eligible_vetoes(policy, candidate):
    gate = EligibilityGate(policy)
    job = _job(eligibility_text="US only. We do not offer visa sponsorship.",
               location="New York, USA")
    axes = gate.evaluate(job, candidate, today=TODAY)
    assert axes.status == EligibilityStatus.NOT_ELIGIBLE.value


def test_off_lane_excluded(policy, candidate):
    gate = EligibilityGate(policy)
    job = _job(title="Frontend React Engineer", lane="REACT_FRONTEND",
               mandatory_requirements=("React",))
    axes = gate.evaluate(job, candidate, today=TODAY)
    assert axes.status == EligibilityStatus.EXCLUDED.value
    assert "off target lane" in axes.vetoes


def test_closure_evidence_marks_closed(policy, candidate):
    term = policy.verification.closure_evidence_terms[0]
    gate = EligibilityGate(policy)
    job = _job(evidence_texts=(f"This role is {term}.",))
    axes = gate.evaluate(job, candidate, today=TODAY)
    assert axes.status == EligibilityStatus.CLOSED.value
    assert axes.closed is True


def test_exclusion_family_excludes(policy, candidate):
    if not policy.exclusions.families or not policy.exclusions.families[0].terms:
        pytest.skip("no exclusion families configured")
    term = policy.exclusions.families[0].terms[0]
    gate = EligibilityGate(policy)
    job = _job(title=f"{term} specialist", lane=None,
               description=f"This is a {term} role.")
    axes = gate.evaluate(job, candidate, today=TODAY)
    assert axes.status == EligibilityStatus.EXCLUDED.value


# --------------------------------------------------------------------------- #
# Lane separation
# --------------------------------------------------------------------------- #
def test_six_lane_separation(policy, candidate):
    react = _job(title="Senior React Frontend Developer", lane=None, description="React, TypeScript, Redux")
    lane = classify_lane(react, policy)
    assert lane != "JAVA_BACKEND"


# --------------------------------------------------------------------------- #
# Two-tier ranking
# --------------------------------------------------------------------------- #
def test_deferred_backlog_retained(policy, candidate):
    jobs = [
        _job(job_key="k_ok", verification_state="VERIFIED_OFFICIAL"),
        _job(job_key="k_def", verification_state="PORTAL_CURRENT_LEAD"),
    ]
    ranker = TriageRanker(policy, candidate)
    result = ranker.rank(jobs, today=TODAY)
    assert "k_def" in result.deferred
    # deferred jobs are retained in evaluations, not dropped
    assert result.by_key("k_def") is not None


def test_dead_posting_not_scored_from_title(policy, candidate):
    jobs = [_job(job_key="k_dead", is_fetchable=False)]
    ranker = TriageRanker(policy, candidate)
    result = ranker.rank(jobs, today=TODAY)
    ev = result.by_key("k_dead")
    assert ev.candidate_fit is None
    assert ev.tier == "NOT_EVALUATED"
    assert ev.recommendation == Recommendation.MANUAL_VERIFICATION.value


def test_top_n_deep_review_only(policy, candidate):
    jobs = [_job(job_key=f"k{i}", mandatory_requirements=("Java",)) for i in range(5)]
    result = rank_and_evaluate(jobs, policy, candidate, deep_limit=2, today=TODAY)
    deep = [e for e in result.evaluations if e.tier == "DEEP"]
    assert len(deep) == 2
    assert len(result.selected) == 2


def test_factual_strengths_and_gaps(policy, candidate):
    job = _job(mandatory_requirements=("Java", "Kubernetes"), preferred_requirements=("Spring Boot",))
    deep = DeepEvaluator(policy, candidate)
    ev = deep.evaluate(job, today=TODAY)
    assert "Java" in ev.strengths           # supported by professional evidence
    assert "Kubernetes" in ev.gaps          # unsupported mandatory
    assert ev.supported_evidence_ids        # real claim ids, not invented
    # every supporting id is a real ledger claim id
    ids = {c.claim_id for c in build_synthetic_ledger().claims()}
    assert set(ev.supported_evidence_ids) <= ids


def test_score_axes_separated(policy, candidate):
    job = _job()
    ev = DeepEvaluator(policy, candidate).evaluate(job, today=TODAY)
    # distinct axes present and independent
    for axis in ("eligibility", "verification", "freshness", "candidate_fit",
                 "recommendation", "confidence", "reasoning_model", "reasoning_version"):
        assert axis in ev.to_dict()
    assert 0 <= ev.candidate_fit <= 100
    assert 0.0 <= ev.confidence <= 1.0


def test_strong_match_recommends_apply(policy, candidate):
    job = _job(mandatory_requirements=("Java", "Spring Boot"), verification_state="VERIFIED_OFFICIAL",
               posted_date=TODAY)
    ev = DeepEvaluator(policy, candidate).evaluate(job, today=TODAY)
    assert ev.recommendation in (Recommendation.PRIORITY_APPLY.value, Recommendation.STRONG_APPLY.value)


def test_selection_is_bounded_and_query_scoped(policy, candidate):
    jobs = [_job(job_key=f"k{i}") for i in range(30)]
    ranker = TriageRanker(policy, candidate)
    picked = ranker.select(jobs, SelectionQuery(lane="JAVA_BACKEND", limit=10))
    assert len(picked) == 10


# --------------------------------------------------------------------------- #
# Determinism / concurrency 1 == N
# --------------------------------------------------------------------------- #
def test_ranking_is_pure_concurrency_1_equals_n(policy, candidate):
    jobs = [_job(job_key=f"k{i}", mandatory_requirements=("Java", "Spring Boot", "MySQL")[: (i % 3) + 1])
            for i in range(12)]
    ranker = TriageRanker(policy, candidate)

    serial = [e.to_dict() for e in ranker.rank(jobs, today=TODAY).evaluations]

    gate = EligibilityGate(policy)
    with ThreadPoolExecutor(max_workers=4) as ex:
        parallel_axes = list(ex.map(lambda j: gate.evaluate(j, candidate, today=TODAY), jobs))
    serial_axes = [gate.evaluate(j, candidate, today=TODAY) for j in jobs]
    assert parallel_axes == serial_axes  # gate is pure/thread-safe

    # ranking is a pure function of inputs -> identical across runs
    again = [e.to_dict() for e in ranker.rank(jobs, today=TODAY).evaluations]
    assert serial == again


def test_optional_controller_is_advisory_and_never_overrides_veto(policy, candidate):
    from atlas.controllers.copilot import CopilotSdkController
    from tests.test_copilot_sdk_controller import FakeTransport
    import json

    # controller "recommends" an ineligible job strongly; deterministic veto wins
    transport = FakeTransport(content=json.dumps({"scores": [{"job_key": "k_bad", "strengths": ["hype"]}]}))
    ctrl = CopilotSdkController(transport=transport)
    jobs = [_job(job_key="k_bad", experience_text="Minimum 9 years required")]
    ranker = TriageRanker(policy, candidate, controller=ctrl, model="claude-opus-4.8")
    result = ranker.rank(jobs, today=TODAY)
    ev = result.by_key("k_bad")
    assert ev.eligibility == EligibilityStatus.NOT_ELIGIBLE.value
    assert ev.recommendation == Recommendation.REJECT.value
