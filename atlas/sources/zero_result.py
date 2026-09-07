"""Atlas zero-result safety + bounded sentinel probing (Phase 1A).

A reachable source that returns zero results must NOT be automatically
believed to mean "no jobs exist". This module deterministically classifies a
zero yield as one of:

    * TRUSTED_ZERO          — genuinely no matches, trustworthy
    * UNTRUSTED_ZERO        — zero, but suspicious (health/history/structure)
    * EXTRACTION_UNRESOLVED — could not even tell (parse failed)

and provides the FRAMEWORK for AT MOST ONE bounded sentinel probe when a
zero is untrusted. There is no retry loop: probe once, classify, stop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from atlas.models import ErrorCategory
from atlas.sources.adapter import AdapterError, CapabilityNotSupported, SourceAdapter
from atlas.sources.health import (
    HealthEvidence,
    SourceHealth,
    SourceHealthState,
    UNTRUSTWORTHY_STATES,
    classify_health,
)
from atlas.sources.models import SearchRequest, SearchResult, ZeroResultKind


def health_state_for_category(category: ErrorCategory) -> SourceHealthState:
    """Map a failure category to the health state it implies."""
    return {
        ErrorCategory.HTTP_429: SourceHealthState.RATE_LIMITED,
        ErrorCategory.HTTP_5XX: SourceHealthState.SOURCE_UNAVAILABLE,
        ErrorCategory.SOURCE_UNAVAILABLE: SourceHealthState.SOURCE_UNAVAILABLE,
        ErrorCategory.PARSE_FAILURE: SourceHealthState.SELECTOR_DRIFT_SUSPECTED,
        ErrorCategory.INVALID_RESPONSE: SourceHealthState.SELECTOR_DRIFT_SUSPECTED,
        ErrorCategory.SELECTOR_UNCERTAINTY: SourceHealthState.SELECTOR_DRIFT_SUSPECTED,
        ErrorCategory.ANTI_BOT: SourceHealthState.ACCESS_LIMITED,
        ErrorCategory.LOGIN_WALL: SourceHealthState.AUTH_REQUIRED,
        ErrorCategory.CAPTCHA: SourceHealthState.ACCESS_LIMITED,
        ErrorCategory.MFA: SourceHealthState.AUTH_REQUIRED,
    }.get(category, SourceHealthState.UNKNOWN)


def assess_search(
    result: SearchResult,
    *,
    health: Optional[SourceHealth] = None,
    historical_yields: Sequence[int] = (),
) -> ZeroResultKind:
    """Classify a search result's zero-ness deterministically.

    An adapter's own ``zero_result_kind`` is respected when it already
    signals EXTRACTION_UNRESOLVED or UNTRUSTED_ZERO; health and historical
    yield can only *escalate* a trusted zero to untrusted, never the reverse.
    """
    if result.count > 0:
        return ZeroResultKind.NOT_APPLICABLE

    # Adapter could not parse → extraction unresolved dominates.
    if result.zero_result_kind == ZeroResultKind.EXTRACTION_UNRESOLVED:
        return ZeroResultKind.EXTRACTION_UNRESOLVED
    if result.parse_findings and result.count == 0 and result.total_reported in (None, 0):
        # Parse produced only findings and nothing else — unresolved.
        if result.zero_result_kind != ZeroResultKind.TRUSTED_ZERO:
            return ZeroResultKind.EXTRACTION_UNRESOLVED

    if result.zero_result_kind == ZeroResultKind.UNTRUSTED_ZERO:
        return ZeroResultKind.UNTRUSTED_ZERO

    # Health-based escalation.
    if health is not None and health.state in UNTRUSTWORTHY_STATES:
        return ZeroResultKind.UNTRUSTED_ZERO

    # Historical-yield collapse: high past yield, zero now → suspicious.
    if historical_yields and max(historical_yields) >= 1:
        return ZeroResultKind.UNTRUSTED_ZERO

    return ZeroResultKind.TRUSTED_ZERO


def should_run_sentinel(kind: ZeroResultKind) -> bool:
    """Only an UNTRUSTED_ZERO warrants a (single) sentinel probe."""
    return kind == ZeroResultKind.UNTRUSTED_ZERO


@dataclass
class SentinelOutcome:
    ran: bool
    kind: ZeroResultKind
    health: SourceHealth
    detail: str

    def to_dict(self) -> dict:
        return {
            "ran": self.ran,
            "kind": self.kind.value,
            "health": self.health.to_dict(),
            "detail": self.detail,
        }


def run_sentinel_probe(
    adapter: SourceAdapter,
    sentinel_request: SearchRequest,
    *,
    historical_yields: Sequence[int] = (),
) -> SentinelOutcome:
    """Run AT MOST ONE broad sentinel query and classify source health.

    Never loops, never retries. A structurally-successful sentinel that
    yields results means the original zero was query-specific (trusted); a
    sentinel that also yields zero with intact structure is a trusted zero;
    anything else escalates to an untrusted/degraded verdict.
    """
    try:
        probe = adapter.search(sentinel_request)
    except CapabilityNotSupported:
        return SentinelOutcome(
            ran=False,
            kind=ZeroResultKind.UNTRUSTED_ZERO,
            health=SourceHealth(SourceHealthState.UNKNOWN, "Sentinel not possible: no SEARCH capability."),
            detail="adapter does not support search",
        )
    except AdapterError as exc:
        state = health_state_for_category(exc.category)
        kind = (
            ZeroResultKind.EXTRACTION_UNRESOLVED
            if state == SourceHealthState.SELECTOR_DRIFT_SUSPECTED
            else ZeroResultKind.UNTRUSTED_ZERO
        )
        return SentinelOutcome(
            ran=True,
            kind=kind,
            health=SourceHealth(state, f"Sentinel probe failed: {exc.category.value}."),
            detail=exc.message,
        )

    if probe.count > 0:
        return SentinelOutcome(
            ran=True,
            kind=ZeroResultKind.TRUSTED_ZERO,
            health=SourceHealth(SourceHealthState.HEALTHY, "Sentinel returned results; source structurally healthy."),
            detail=f"sentinel yielded {probe.count} results; original zero is query-specific",
        )

    evidence = HealthEvidence(
        result_count=0,
        historical_yields=tuple(historical_yields),
        expected_structure_present=(not probe.parse_findings),
        parse_failure_ratio=(1.0 if probe.parse_findings else 0.0),
    )
    health = classify_health(evidence)
    kind = ZeroResultKind.TRUSTED_ZERO if health.state == SourceHealthState.HEALTHY else ZeroResultKind.UNTRUSTED_ZERO
    return SentinelOutcome(
        ran=True,
        kind=kind,
        health=health,
        detail="sentinel also returned zero results",
    )


__all__ = [
    "health_state_for_category",
    "assess_search",
    "should_run_sentinel",
    "SentinelOutcome",
    "run_sentinel_probe",
]
