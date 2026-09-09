"""Failing-first regression: India gate in evaluate_detail + geo-aware matching (audit 3.2/3.3/3.8)."""

from __future__ import annotations

import datetime

import pytest

from atlas.candidate.eligibility import Recommendation
from atlas.hunt.geography import GeoDecision, classify_job_geography
from atlas.hunt.matching import APPLY_FAMILY, HuntCandidate, match_qualified
from atlas.hunt.models import JobDetailRevision
from atlas.hunt.pipeline import evaluate_detail
from atlas.hunt.role_intent import load_role_intent_policy
from atlas.policy.loader import load_policy

TODAY = datetime.date(2026, 9, 9)


@pytest.fixture(scope="module")
def policy():
    return load_policy()


@pytest.fixture(scope="module")
def intent():
    return load_role_intent_policy()


def _detail(location, *, reqs=("Java", "Spring Boot"), title="Java Backend Engineer",
            desc="Build Java Spring Boot microservices and REST APIs.", exp="2 years"):
    return JobDetailRevision(
        revision_id="R1", snapshot_id="S1", source_job_id="J1", company="Acme",
        title=title, description=desc, mandatory_requirements=tuple(reqs), experience_text=exp,
        location=location, posted_date=TODAY, official_url="https://acme.example/careers/j1",
        requisition_id="REQ1", verification_state="VERIFIED_OFFICIAL", has_live_official_page=True,
        eligibility_text=f"{location}. {desc}", source_family="OFFICIAL_CAREERS",
    )


def test_foreign_job_rejected_by_evaluate_detail(policy, intent):
    ev = evaluate_detail(_detail("San Francisco, CA"), intent, policy, candidate_years=2.0, today=TODAY)
    assert ev.final_status == "REJECT_LOCATION"
    assert ev.geo_decision.decision == GeoDecision.FOREIGN_EXCLUDED.value


def test_unknown_location_rejected_by_evaluate_detail(policy, intent):
    ev = evaluate_detail(_detail("N/A"), intent, policy, candidate_years=2.0, today=TODAY)
    assert ev.final_status == "REJECT_LOCATION"
    assert ev.geo_decision.decision == GeoDecision.UNKNOWN_LOCATION.value


def test_india_job_qualifies(policy, intent):
    ev = evaluate_detail(_detail("Bengaluru, India"), intent, policy, candidate_years=2.0, today=TODAY)
    assert ev.final_status == "QUALIFIED"
    assert ev.geo_decision.india_eligible is True


def test_matching_location_score_from_job_not_candidate(policy, intent):
    ev = evaluate_detail(_detail("Bengaluru, India"), intent, policy, candidate_years=2.0, today=TODAY)
    cand = HuntCandidate.from_skills(["Java", "Spring Boot"], location_group="PRIMARY",
                                     target_lanes=("JAVA_BACKEND",))
    m = match_qualified(ev.detail, ev.qualification.by_lane[ev.final_lane], cand, today=TODAY, geo=ev.geo_decision)
    assert m.requirements_matched  # non-empty requirement evidence
    assert m.recommendation in APPLY_FAMILY


def test_empty_requirements_cannot_be_apply_family(policy, intent):
    d = _detail("Bengaluru, India", reqs=())
    ev = evaluate_detail(d, intent, policy, candidate_years=2.0, today=TODAY)
    cand = HuntCandidate.from_skills(["Java", "Spring Boot"], target_lanes=("JAVA_BACKEND",))
    m = match_qualified(ev.detail, ev.qualification.by_lane[ev.final_lane], cand, today=TODAY, geo=ev.geo_decision)
    assert m.recommendation not in APPLY_FAMILY
