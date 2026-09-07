"""Atlas source-health model (Phase 1A).

A source can be *reachable yet broken*: the site returns HTTP 200 but the
extractor is silently producing nothing ("site up + selector drift = 'no
jobs'"). This module models health as a first-class diagnostic that is
deliberately SEPARATE from task/run lifecycle state (``atlas.models.TaskStatus``):

    * TaskStatus answers "what happened to this task?" (SUCCESS, RETRY, ...).
    * SourceHealth answers "can we currently trust this source's output?"

Keeping them separate avoids polluting the task lifecycle with every
possible diagnostic condition. A degraded source may still have produced a
valid partial task result, and a healthy source may still legitimately
return zero matches.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence


class SourceHealthState(str, enum.Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    SELECTOR_DRIFT_SUSPECTED = "SELECTOR_DRIFT_SUSPECTED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"
    ACCESS_LIMITED = "ACCESS_LIMITED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


# States that mean "do not trust a zero/low yield from this source as proof
# that no jobs exist".
UNTRUSTWORTHY_STATES: frozenset[SourceHealthState] = frozenset(
    {
        SourceHealthState.DEGRADED,
        SourceHealthState.SELECTOR_DRIFT_SUSPECTED,
        SourceHealthState.AUTH_REQUIRED,
        SourceHealthState.RATE_LIMITED,
        SourceHealthState.ACCESS_LIMITED,
        SourceHealthState.SOURCE_UNAVAILABLE,
        SourceHealthState.UNKNOWN,
    }
)


@dataclass(frozen=True)
class HealthEvidence:
    """Observed signals that feed a health classification. Every field is
    optional — a lightweight probe may only fill in a few. Nothing here is
    ever invented; unknown stays ``None``."""

    http_status: Optional[int] = None
    expected_structure_present: Optional[bool] = None
    result_count: Optional[int] = None
    historical_yields: tuple[int, ...] = ()
    null_field_ratio: Optional[float] = None
    parse_failure_ratio: Optional[float] = None
    duplicate_ratio: Optional[float] = None
    unexpected_redirect: Optional[bool] = None
    login_redirect: Optional[bool] = None
    challenge_detected: Optional[bool] = None
    schema_drift: Optional[bool] = None
    undecoded_entities: Optional[bool] = None
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "http_status": self.http_status,
            "expected_structure_present": self.expected_structure_present,
            "result_count": self.result_count,
            "historical_yields": list(self.historical_yields),
            "null_field_ratio": self.null_field_ratio,
            "parse_failure_ratio": self.parse_failure_ratio,
            "duplicate_ratio": self.duplicate_ratio,
            "unexpected_redirect": self.unexpected_redirect,
            "login_redirect": self.login_redirect,
            "challenge_detected": self.challenge_detected,
            "schema_drift": self.schema_drift,
            "undecoded_entities": self.undecoded_entities,
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }


class SourceHealth:
    """Typed health verdict for a source.

    Backward compatible with the Phase 0.5 boolean contract: constructing
    ``SourceHealth(healthy=True, detail="...")`` still works and
    ``.healthy`` still returns a bool (True only when state is HEALTHY).
    """

    __slots__ = ("state", "detail", "evidence", "metadata")

    def __init__(
        self,
        state: SourceHealthState = SourceHealthState.UNKNOWN,
        detail: str = "",
        evidence: Optional[HealthEvidence] = None,
        metadata: Optional[dict[str, Any]] = None,
        *,
        healthy: Optional[bool] = None,
    ) -> None:
        if healthy is not None:
            state = SourceHealthState.HEALTHY if healthy else SourceHealthState.DEGRADED
        self.state = state
        self.detail = detail
        self.evidence = evidence
        self.metadata = dict(metadata or {})

    @property
    def healthy(self) -> bool:
        return self.state == SourceHealthState.HEALTHY

    @property
    def trustworthy(self) -> bool:
        """True when a zero/low yield from this source can be believed."""
        return self.state == SourceHealthState.HEALTHY

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "healthy": self.healthy,
            "detail": self.detail,
            "evidence": self.evidence.to_dict() if self.evidence else None,
            "metadata": dict(self.metadata),
        }

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SourceHealth):
            return NotImplemented
        return (
            self.state == other.state
            and self.detail == other.detail
            and self.metadata == other.metadata
        )

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"SourceHealth(state={self.state.value!r}, detail={self.detail!r})"


def classify_health(
    evidence: HealthEvidence,
    *,
    historical_yields: Optional[Sequence[int]] = None,
) -> SourceHealth:
    """Deterministically classify a source's health from observed evidence.

    Ordering matters: security/access conditions dominate, then structural
    drift, then a sudden yield collapse relative to history, then generic
    quality degradation. When nothing is suspicious the verdict is HEALTHY.
    """
    yields = tuple(historical_yields) if historical_yields is not None else evidence.historical_yields

    # 1. Access / auth / rate conditions (never a bypass; just diagnosis).
    if evidence.challenge_detected:
        return SourceHealth(SourceHealthState.ACCESS_LIMITED, "Challenge/anti-bot page detected.", evidence)
    if evidence.login_redirect:
        return SourceHealth(SourceHealthState.AUTH_REQUIRED, "Redirected to a login/auth wall.", evidence)
    if evidence.http_status == 429:
        return SourceHealth(SourceHealthState.RATE_LIMITED, "HTTP 429 rate limiting observed.", evidence)
    if evidence.http_status is not None and 500 <= evidence.http_status < 600:
        return SourceHealth(SourceHealthState.SOURCE_UNAVAILABLE, f"HTTP {evidence.http_status} from source.", evidence)

    # 2. Structural drift (schema/markers changed, entities not decoding).
    if evidence.expected_structure_present is False or evidence.schema_drift:
        return SourceHealth(
            SourceHealthState.SELECTOR_DRIFT_SUSPECTED,
            "Expected structural markers missing / schema drift.",
            evidence,
        )
    if evidence.undecoded_entities:
        return SourceHealth(
            SourceHealthState.SELECTOR_DRIFT_SUSPECTED,
            "Undecoded entities suggest a parser/encoding mismatch.",
            evidence,
        )
    if evidence.parse_failure_ratio is not None and evidence.parse_failure_ratio >= 0.5:
        return SourceHealth(
            SourceHealthState.SELECTOR_DRIFT_SUSPECTED,
            f"High parse-failure ratio ({evidence.parse_failure_ratio:.2f}).",
            evidence,
        )

    # 3. Sudden yield collapse vs history (the classic false-zero signal).
    if evidence.result_count == 0 and yields:
        recent_max = max(yields)
        if recent_max >= 1:
            return SourceHealth(
                SourceHealthState.SELECTOR_DRIFT_SUSPECTED,
                f"Zero results now but historical yields were {list(yields)}.",
                evidence,
            )

    if evidence.unexpected_redirect:
        return SourceHealth(SourceHealthState.DEGRADED, "Unexpected redirect observed.", evidence)

    # 4. Generic quality degradation.
    if evidence.null_field_ratio is not None and evidence.null_field_ratio >= 0.5:
        return SourceHealth(
            SourceHealthState.DEGRADED,
            f"High null-field ratio ({evidence.null_field_ratio:.2f}).",
            evidence,
        )
    if evidence.duplicate_ratio is not None and evidence.duplicate_ratio >= 0.9:
        return SourceHealth(
            SourceHealthState.DEGRADED,
            f"Nearly all results duplicated ({evidence.duplicate_ratio:.2f}).",
            evidence,
        )

    return SourceHealth(SourceHealthState.HEALTHY, "No degradation signals detected.", evidence)


__all__ = [
    "SourceHealthState",
    "UNTRUSTWORTHY_STATES",
    "HealthEvidence",
    "SourceHealth",
    "classify_health",
]
