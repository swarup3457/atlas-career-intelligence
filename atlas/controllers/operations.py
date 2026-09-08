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
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

from atlas.controllers.base import Controller, ControllerRequest, NullController

PROMPT_PACK_VERSION = "1B.1"

# Canonical lanes (kept in sync with config/policy/search_lanes.yaml). A caller
# may pass the actual configured lanes; this is the safe default universe.
DEFAULT_LANES: frozenset[str] = frozenset(
    {
        "GENERAL_SOFTWARE",
        "JAVA_BACKEND",
        "JAVA_FULLSTACK",
        "REACT_FRONTEND",
        "DOTNET",
        "ENTERPRISE_HR_PAYROLL_INTEGRATION",
    }
)

# Business verification vocabulary + a strength ordering so an LLM suggestion
# can never EXCEED the deterministic source-evidence ceiling (build spec 20).
_VERIFICATION_LEVELS: tuple[str, ...] = (
    "SUSPICIOUS_REJECTED",
    "MANUAL_VERIFICATION",
    "PORTAL_CURRENT_LEAD",
    "OFFICIAL_PAGE_FOUND_APPLY_PATH_UNCONFIRMED",
    "VERIFIED_OFFICIAL",
)
_VERIFICATION_RANK: dict[str, int] = {name: i for i, name in enumerate(_VERIFICATION_LEVELS)}

_ELIGIBILITY_CLASSES: frozenset[str] = frozenset(
    {"ELIGIBLE_FROM_INDIA", "ELIGIBLE_WITH_EVIDENCE", "UNCLEAR", "NOT_ELIGIBLE"}
)


class ControllerValidationError(ValueError):
    """Raised internally when an LLM controller result violates an operation-
    specific schema/constraint. The operation then falls back to the
    deterministic default and records an ``INVALID_FELL_BACK`` audit finding."""


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
    """Wraps a :class:`Controller` with typed, STRICTLY-VALIDATED operations.

    An LLM result is accepted only if it satisfies the operation-specific
    schema (allowed lanes/levels/classes, confidence in ``[0, 1]``, evidence
    subset constraints, no invented technology, deterministic ceilings). Any
    violation deterministically falls back to the safe default, marks the
    outcome ``INVALID_FELL_BACK`` and appends an audit finding to
    :attr:`audit_findings` for the runtime to persist (build spec 20)."""

    def __init__(
        self,
        controller: Optional[Controller] = None,
        *,
        model: str = "none",
        allowed_lanes: Optional[frozenset[str]] = None,
    ):
        self.controller = controller or NullController()
        self.model = model
        self.allowed_lanes = frozenset(allowed_lanes) if allowed_lanes else DEFAULT_LANES
        self.audit_findings: list[dict[str, Any]] = []

    def _meta(self, outcome: str) -> OperationMetadata:
        return OperationMetadata(
            controller=getattr(self.controller, "name", "unknown"),
            model=self.model,
            prompt_pack_version=PROMPT_PACK_VERSION,
            validation_outcome=outcome,
        )

    def _is_null(self) -> bool:
        return isinstance(self.controller, NullController) or getattr(self.controller, "name", "") == "none"

    def _record_finding(self, operation: str, reason: str) -> str:
        self.audit_findings.append(
            {
                "operation": operation,
                "reason": reason,
                "outcome": "INVALID_FELL_BACK",
                "controller": getattr(self.controller, "name", "unknown"),
                "model": self.model,
                "prompt_pack_version": PROMPT_PACK_VERSION,
            }
        )
        return "INVALID_FELL_BACK"

    def _call(self, method: str, prompt: str, context: dict) -> tuple[Optional[dict], str]:
        """Call the controller and parse JSON content. Returns
        (parsed_or_none, outcome). Operation-specific validation happens in the
        caller; a non-object/JSON error here is already ``INVALID_FELL_BACK``."""
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
        except (ValueError, TypeError) as exc:
            return None, self._record_finding(prompt, f"unparseable controller output: {exc}")

    # -- validators ---------------------------------------------------------
    @staticmethod
    def _confidence(value: Any) -> float:
        c = float(value)
        if not (0.0 <= c <= 1.0):
            raise ControllerValidationError(f"confidence {c} out of range [0,1]")
        return c

    def _lane(self, value: Any, default: str) -> str:
        lane = str(value)
        if lane not in self.allowed_lanes:
            raise ControllerValidationError(f"lane {lane!r} not in configured lanes")
        return lane

    # -- operations ---------------------------------------------------------
    def expand_query(self, req: QueryExpansionRequest) -> QueryExpansionResult:
        parsed, outcome = self._call("reason", "expand_query", asdict(req))
        if parsed is not None and outcome == "VALID":
            try:
                raw = parsed.get("expansions", [])
                if not isinstance(raw, list):
                    raise ControllerValidationError("expansions must be a list")
                expansions = tuple(str(x) for x in raw if str(x).strip())
                if any(len(x) > 128 for x in expansions):
                    raise ControllerValidationError("expansion term too long")
                return QueryExpansionResult(expansions=expansions, metadata=self._meta("VALID"))
            except (ControllerValidationError, ValueError, TypeError) as exc:
                outcome = self._record_finding("expand_query", str(exc))
        return QueryExpansionResult(expansions=tuple(req.base_terms), metadata=self._meta(outcome))

    def classify_role(self, req: RoleClassificationRequest) -> RoleClassificationResult:
        parsed, outcome = self._call("classify", "classify_role", asdict(req))
        if parsed is not None and outcome == "VALID":
            try:
                lane = self._lane(parsed.get("lane", req.lane_hint or "GENERAL_SOFTWARE"),
                                  req.lane_hint or "GENERAL_SOFTWARE")
                return RoleClassificationResult(
                    lane=lane, is_relevant=bool(parsed.get("is_relevant", False)),
                    reason=str(parsed.get("reason", "")), metadata=self._meta("VALID"),
                )
            except (ControllerValidationError, ValueError, TypeError) as exc:
                outcome = self._record_finding("classify_role", str(exc))
        return RoleClassificationResult(
            lane=req.lane_hint or "GENERAL_SOFTWARE", is_relevant=False,
            reason="null-controller default (deterministic gates decide)", metadata=self._meta(outcome),
        )

    def review_verification(
        self, req: VerificationReviewRequest, *, deterministic_ceiling: Optional[str] = None
    ) -> VerificationReviewResult:
        """Optional typed reviewer. The LLM ``suggested_level`` must be a valid
        business level and can NEVER exceed ``deterministic_ceiling`` — the
        deterministic source-evidence verdict is an upper bound the LLM cannot
        strengthen (build spec 20)."""
        parsed, outcome = self._call("review", "review_verification", asdict(req))
        if parsed is not None and outcome == "VALID":
            try:
                level = str(parsed.get("suggested_level", "MANUAL_VERIFICATION"))
                if level not in _VERIFICATION_RANK:
                    raise ControllerValidationError(f"suggested_level {level!r} not allowed")
                confidence = self._confidence(parsed.get("confidence", 0.0))
                if deterministic_ceiling is not None:
                    ceil = _VERIFICATION_RANK.get(deterministic_ceiling)
                    if ceil is None:
                        raise ControllerValidationError(f"invalid ceiling {deterministic_ceiling!r}")
                    if _VERIFICATION_RANK[level] > ceil:
                        raise ControllerValidationError(
                            f"suggested_level {level!r} exceeds deterministic ceiling {deterministic_ceiling!r}"
                        )
                return VerificationReviewResult(
                    suggested_level=level, confidence=confidence,
                    reason=str(parsed.get("reason", "")), metadata=self._meta("VALID"),
                )
            except (ControllerValidationError, ValueError, TypeError) as exc:
                outcome = self._record_finding("review_verification", str(exc))
        # Deterministic default never exceeds the ceiling.
        default_level = deterministic_ceiling or "MANUAL_VERIFICATION"
        return VerificationReviewResult(suggested_level=default_level, metadata=self._meta(outcome))

    def match_candidate(self, req: CandidateMatchRequest) -> CandidateMatchResult:
        parsed, outcome = self._call("reason", "match_candidate", asdict(req))
        if parsed is not None and outcome == "VALID":
            try:
                supported = tuple(str(x) for x in parsed.get("supported", []))
                missing = tuple(str(x) for x in parsed.get("missing", []))
                clar = tuple(str(x) for x in parsed.get("clarification_required", []))
                evidence = set(req.supported_evidence)
                requirements = set(req.job_requirements)
                invented = [s for s in supported if s not in evidence]
                if invented:
                    raise ControllerValidationError(f"supported items not in candidate evidence: {invented}")
                unrequested = [s for s in supported if s not in requirements]
                if unrequested:
                    raise ControllerValidationError(f"supported items not in job requirements: {unrequested}")
                bad_missing = [m for m in missing if m not in requirements]
                if bad_missing:
                    raise ControllerValidationError(f"missing items not in job requirements: {bad_missing}")
                return CandidateMatchResult(
                    supported=supported, missing=missing, clarification_required=clar,
                    metadata=self._meta("VALID"),
                )
            except (ControllerValidationError, ValueError, TypeError) as exc:
                outcome = self._record_finding("match_candidate", str(exc))
        # Deterministic default: supported = evidence ∩ requirements; missing = rest.
        supported = tuple(r for r in req.job_requirements if r in set(req.supported_evidence))
        missing = tuple(r for r in req.job_requirements if r not in set(req.supported_evidence))
        return CandidateMatchResult(supported=supported, missing=missing, metadata=self._meta(outcome))

    def review_eligibility(self, req: EligibilityReviewRequest) -> EligibilityReviewResult:
        parsed, outcome = self._call("review", "review_eligibility", asdict(req))
        if parsed is not None and outcome == "VALID":
            try:
                classification = str(parsed.get("classification", "UNCLEAR"))
                if classification not in _ELIGIBILITY_CLASSES:
                    raise ControllerValidationError(f"classification {classification!r} not allowed")
                return EligibilityReviewResult(
                    classification=classification,
                    supporting_wording=str(parsed.get("supporting_wording", "")),
                    metadata=self._meta("VALID"),
                )
            except (ControllerValidationError, ValueError, TypeError) as exc:
                outcome = self._record_finding("review_eligibility", str(exc))
        return EligibilityReviewResult(metadata=self._meta(outcome))

    def write_application_brief(self, req: ApplicationBriefRequest) -> ApplicationBriefResult:
        parsed, outcome = self._call("reason", "write_application_brief", asdict(req))
        if parsed is not None and outcome == "VALID":
            try:
                summary = str(parsed.get("summary", ""))
                do_not_claim = tuple(str(x) for x in parsed.get("do_not_claim", []))
                # The brief must never assert a MISSING point as a strength.
                low = summary.lower()
                asserted_missing = [
                    m for m in req.missing_points
                    if m and re.search(r"\b" + re.escape(m.lower()) + r"\b", low)
                ]
                if asserted_missing:
                    raise ControllerValidationError(
                        f"brief asserts unsupported/missing points: {asserted_missing}"
                    )
                return ApplicationBriefResult(
                    summary=summary, do_not_claim=do_not_claim, metadata=self._meta("VALID")
                )
            except (ControllerValidationError, ValueError, TypeError) as exc:
                outcome = self._record_finding("write_application_brief", str(exc))
        return ApplicationBriefResult(
            summary=f"{req.role} at {req.company}", do_not_claim=tuple(req.missing_points),
            metadata=self._meta(outcome),
        )


__all__ = [
    "PROMPT_PACK_VERSION",
    "DEFAULT_LANES",
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
