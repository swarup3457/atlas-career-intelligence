"""Phase 1B.1 — strict typed controller result validation (build spec 20 / P0-19).

An LLM controller result is accepted ONLY when it satisfies the operation-
specific schema. Any violation deterministically falls back to the safe
default, marks the outcome INVALID_FELL_BACK, and records an audit finding.
"""

from __future__ import annotations

import json

import pytest

from atlas.controllers.base import BaseController, ControllerRequest, ControllerResponse
from atlas.controllers.operations import (
    ApplicationBriefRequest,
    CandidateMatchRequest,
    EligibilityReviewRequest,
    RoleClassificationRequest,
    TypedControllerOps,
    VerificationReviewRequest,
)

pytestmark = pytest.mark.unit


class ScriptedController(BaseController):
    """Returns a fixed JSON payload for every operation (a stub LLM)."""

    name = "scripted"

    def __init__(self, payload: dict):
        self._content = json.dumps(payload)

    def _resp(self, _req: ControllerRequest) -> ControllerResponse:
        return ControllerResponse(content=self._content, metadata={"controller": self.name})

    reason = classify = extract = review = _resp


def _ops(payload: dict) -> TypedControllerOps:
    return TypedControllerOps(ScriptedController(payload), model="scripted")


def test_match_cannot_invent_supported_technology():
    ops = _ops({"supported": ["Java", "AWS"], "missing": []})
    res = ops.match_candidate(
        CandidateMatchRequest(job_requirements=("Java", "AWS"), supported_evidence=("Java",))
    )
    # AWS is not in the candidate evidence -> the whole result is rejected and
    # the deterministic default (supported = evidence ∩ requirements) is used.
    assert res.supported == ("Java",)
    assert res.missing == ("AWS",)
    assert res.metadata.validation_outcome == "INVALID_FELL_BACK"
    assert ops.audit_findings and ops.audit_findings[0]["operation"] == "match_candidate"


def test_match_missing_must_be_subset_of_requirements():
    ops = _ops({"supported": ["Java"], "missing": ["Kubernetes"]})
    res = ops.match_candidate(
        CandidateMatchRequest(job_requirements=("Java", "AWS"), supported_evidence=("Java",))
    )
    assert res.metadata.validation_outcome == "INVALID_FELL_BACK"


def test_valid_match_is_accepted():
    ops = _ops({"supported": ["Java"], "missing": ["AWS"]})
    res = ops.match_candidate(
        CandidateMatchRequest(job_requirements=("Java", "AWS"), supported_evidence=("Java", "Docker"))
    )
    assert res.supported == ("Java",) and res.missing == ("AWS",)
    assert res.metadata.validation_outcome == "VALID"
    assert not ops.audit_findings


def test_confidence_out_of_range_falls_back():
    ops = _ops({"suggested_level": "MANUAL_VERIFICATION", "confidence": 1.7})
    res = ops.review_verification(
        VerificationReviewRequest(page_kind="portal", identity_aligned=True, current_content=False)
    )
    assert res.metadata.validation_outcome == "INVALID_FELL_BACK"


def test_llm_cannot_exceed_deterministic_verification_ceiling():
    ops = _ops({"suggested_level": "VERIFIED_OFFICIAL", "confidence": 0.9})
    res = ops.review_verification(
        VerificationReviewRequest(page_kind="portal", identity_aligned=True, current_content=False),
        deterministic_ceiling="PORTAL_CURRENT_LEAD",
    )
    # The LLM cannot strengthen the verdict beyond the deterministic ceiling.
    assert res.suggested_level == "PORTAL_CURRENT_LEAD"
    assert res.metadata.validation_outcome == "INVALID_FELL_BACK"


def test_unknown_lane_rejected():
    ops = _ops({"lane": "QUANTUM_COMPUTING", "is_relevant": True})
    res = ops.classify_role(RoleClassificationRequest(title="X", lane_hint="JAVA_BACKEND"))
    assert res.lane == "JAVA_BACKEND"
    assert res.metadata.validation_outcome == "INVALID_FELL_BACK"


def test_eligibility_classification_must_be_allowed():
    ops = _ops({"classification": "DEFINITELY_YES"})
    res = ops.review_eligibility(EligibilityReviewRequest(location_text="Remote"))
    assert res.classification == "UNCLEAR"
    assert res.metadata.validation_outcome == "INVALID_FELL_BACK"


def test_application_brief_cannot_assert_missing_points():
    ops = _ops({"summary": "Strong Kubernetes and Java expertise.", "do_not_claim": []})
    res = ops.write_application_brief(
        ApplicationBriefRequest(company="Acme", role="SWE",
                                supported_points=("Java",), missing_points=("Kubernetes",))
    )
    assert res.metadata.validation_outcome == "INVALID_FELL_BACK"
    assert "Kubernetes" in res.do_not_claim


def test_malformed_json_falls_back():
    class BadController(BaseController):
        name = "bad"

        def _resp(self, _req):
            return ControllerResponse(content="{not json")

        reason = classify = extract = review = _resp

    ops = TypedControllerOps(BadController(), model="bad")
    res = ops.match_candidate(CandidateMatchRequest(job_requirements=("Java",), supported_evidence=("Java",)))
    assert res.supported == ("Java",)
    assert res.metadata.validation_outcome == "INVALID_FELL_BACK"
