"""Typed controller operations (Phase 1B, build spec 7.13).

Production reasoning uses operation-specific typed request/result schemas
instead of generic free-text ``ControllerResponse.content``. Each operation:

    * builds a typed request,
    * calls the underlying :class:`Controller` (transport stays isolated and
      optional),
    * validates the JSON/schema of the response,
    * records controller name, model, prompt-pack version, and the response
      validation outcome.

Deterministic execution MUST still work with :class:`NullController`: every
operation returns a safe, typed default whose ``validation_outcome`` is
``NULL_CONTROLLER_DEFAULT`` and whose result never fabricates evidence.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from atlas.controllers.base import Controller, ControllerRequest, NullController

PROMPT_PACK_VERSION = "1B.1"


class ControllerValidationError(ValueError):
    pass


@dataclass
class OperationMetadata:
    controller: str
    model: str
    prompt_pack_version: str
    validation_outcome: str  # VALID | INVALID_FELL_BACK | NULL_CONTROLLER_DEFAULT

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- requests --------------------------------------------------------------
@dataclass
class QueryExpansionRequest:
    lane: str
    base_terms: tuple[str, ...] = ()
    geography_group: str = "PRIMARY"


@dataclass
class QueryExpansionResult:
    expansions: tuple[str, ...] = ()
    metadata: Optional[OperationMetadata] = None


@dataclass
class RoleClassificationRequest:
    title: str
    description: str = ""
    lane_hint: str = ""


@dataclass
class RoleClassificationResult:
    lane: str = "GENERAL_SOFTWARE"
    is_relevant: bool = False
    reason: str = ""
    metadata: Optional[OperationMetadata] = None


@dataclass
class VerificationReviewRequest:
    page_kind: str
    identity_aligned: bool
    current_content: bool
    ambiguous_signals: tuple[str, ...] = ()


@dataclass
class VerificationReviewResult:
    suggested_level: str = "MANUAL_VERIFICATION"
    confidence: float = 0.0
    reason: str = ""
    metadata: Optional[OperationMetadata] = None


@dataclass
class CandidateMatchRequest:
    job_requirements: tuple[str, ...] = ()
    supported_evidence: tuple[str, ...] = ()
    lane: str = "GENERAL_SOFTWARE"


@dataclass
class CandidateMatchResult:
    supported: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    clarification_required: tuple[str, ...] = ()
    metadata: Optional[OperationMetadata] = None


@dataclass
class EligibilityReviewRequest:
    location_text: str = ""
    sponsorship_text: str = ""


@dataclass
class EligibilityReviewResult:
    classification: str = "UNCLEAR"
    supporting_wording: str = ""
    metadata: Optional[OperationMetadata] = None


@dataclass
class ApplicationBriefRequest:
    company: str
    role: str
    supported_points: tuple[str, ...] = ()
    missing_points: tuple[str, ...] = ()


@dataclass
class ApplicationBriefResult:
    summary: str = ""
    do_not_claim: tuple[str, ...] = ()
    metadata: Optional[OperationMetadata] = None


class TypedControllerOps:
    """Wraps a :class:`Controller` with typed, validated operations."""

    def __init__(self, controller: Optional[Controller] = None, *, model: str = "none"):
        self.controller = controller or NullController()
        self.model = model

    def _meta(self, outcome: str) -> OperationMetadata:
        return OperationMetadata(
            controller=getattr(self.controller, "name", "unknown"),
            model=self.model,
            prompt_pack_version=PROMPT_PACK_VERSION,
            validation_outcome=outcome,
        )

    def _is_null(self) -> bool:
        return isinstance(self.controller, NullController) or getattr(self.controller, "name", "") == "none"

    def _call(self, method: str, prompt: str, context: dict) -> tuple[Optional[dict], str]:
        """Call the controller and parse+validate JSON content. Returns
        (parsed_or_none, outcome)."""
        if self._is_null():
            return None, "NULL_CONTROLLER_DEFAULT"
        fn = getattr(self.controller, method)
        resp = fn(ControllerRequest(prompt=prompt, context=context))
        content = (resp.content or "").strip()
        if not content:
            return None, "NULL_CONTROLLER_DEFAULT"
        try:
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise ValueError("expected a JSON object")
            return parsed, "VALID"
        except (ValueError, TypeError):
            return None, "INVALID_FELL_BACK"

    # -- operations ---------------------------------------------------------
    def expand_query(self, req: QueryExpansionRequest) -> QueryExpansionResult:
        parsed, outcome = self._call("reason", "expand_query", asdict(req))
        expansions = tuple(parsed.get("expansions", [])) if parsed else req.base_terms
        return QueryExpansionResult(expansions=tuple(expansions), metadata=self._meta(outcome))

    def classify_role(self, req: RoleClassificationRequest) -> RoleClassificationResult:
        parsed, outcome = self._call("classify", "classify_role", asdict(req))
        if parsed:
            return RoleClassificationResult(
                lane=str(parsed.get("lane", req.lane_hint or "GENERAL_SOFTWARE")),
                is_relevant=bool(parsed.get("is_relevant", False)),
                reason=str(parsed.get("reason", "")),
                metadata=self._meta(outcome),
            )
        return RoleClassificationResult(
            lane=req.lane_hint or "GENERAL_SOFTWARE", is_relevant=False,
            reason="null-controller default (deterministic gates decide)", metadata=self._meta(outcome),
        )

    def review_verification(self, req: VerificationReviewRequest) -> VerificationReviewResult:
        parsed, outcome = self._call("review", "review_verification", asdict(req))
        if parsed:
            return VerificationReviewResult(
                suggested_level=str(parsed.get("suggested_level", "MANUAL_VERIFICATION")),
                confidence=float(parsed.get("confidence", 0.0)),
                reason=str(parsed.get("reason", "")),
                metadata=self._meta(outcome),
            )
        return VerificationReviewResult(metadata=self._meta(outcome))

    def match_candidate(self, req: CandidateMatchRequest) -> CandidateMatchResult:
        parsed, outcome = self._call("reason", "match_candidate", asdict(req))
        if parsed:
            return CandidateMatchResult(
                supported=tuple(parsed.get("supported", [])),
                missing=tuple(parsed.get("missing", [])),
                clarification_required=tuple(parsed.get("clarification_required", [])),
                metadata=self._meta(outcome),
            )
        # Deterministic default: supported = evidence ∩ requirements; missing = rest.
        supported = tuple(r for r in req.job_requirements if r in set(req.supported_evidence))
        missing = tuple(r for r in req.job_requirements if r not in set(req.supported_evidence))
        return CandidateMatchResult(supported=supported, missing=missing, metadata=self._meta(outcome))

    def review_eligibility(self, req: EligibilityReviewRequest) -> EligibilityReviewResult:
        parsed, outcome = self._call("review", "review_eligibility", asdict(req))
        if parsed:
            return EligibilityReviewResult(
                classification=str(parsed.get("classification", "UNCLEAR")),
                supporting_wording=str(parsed.get("supporting_wording", "")),
                metadata=self._meta(outcome),
            )
        return EligibilityReviewResult(metadata=self._meta(outcome))

    def write_application_brief(self, req: ApplicationBriefRequest) -> ApplicationBriefResult:
        parsed, outcome = self._call("reason", "write_application_brief", asdict(req))
        if parsed:
            return ApplicationBriefResult(
                summary=str(parsed.get("summary", "")),
                do_not_claim=tuple(parsed.get("do_not_claim", [])),
                metadata=self._meta(outcome),
            )
        return ApplicationBriefResult(
            summary=f"{req.role} at {req.company}", do_not_claim=(), metadata=self._meta(outcome)
        )


__all__ = [
    "PROMPT_PACK_VERSION",
    "ControllerValidationError",
    "OperationMetadata",
    "TypedControllerOps",
    "QueryExpansionRequest", "QueryExpansionResult",
    "RoleClassificationRequest", "RoleClassificationResult",
    "VerificationReviewRequest", "VerificationReviewResult",
    "CandidateMatchRequest", "CandidateMatchResult",
    "EligibilityReviewRequest", "EligibilityReviewResult",
    "ApplicationBriefRequest", "ApplicationBriefResult",
]
