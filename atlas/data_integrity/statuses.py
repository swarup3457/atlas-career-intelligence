"""Auditable statuses and legal transitions.

Two orthogonal status axes:

* :class:`RecordStatus` — the *pipeline lifecycle* of an ingestion record
  (INGESTED -> NORMALIZED -> VALIDATED -> ... -> CANONICAL / QUARANTINED).
* :class:`CanonicalStatus` — the *business state* of a canonical job as it
  is observed over time (ACTIVE / CLOSED / SUPERSEDED / QUARANTINED /
  UNKNOWN).

Transitions are explicit and validated: illegal transitions raise
:class:`StatusTransitionError`, and every applied transition yields a
:class:`StatusTransition` audit record (who/from/to/why/when) so the full
history is reconstructable.
"""

from __future__ import annotations

import datetime
import enum
from dataclasses import dataclass, field
from typing import Any, Optional


class RecordStatus(str, enum.Enum):
    INGESTED = "INGESTED"
    NORMALIZED = "NORMALIZED"
    VALIDATED = "VALIDATED"
    QUARANTINED = "QUARANTINED"
    RECONCILED = "RECONCILED"
    CANONICAL = "CANONICAL"
    REJECTED = "REJECTED"


class CanonicalStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    SUPERSEDED = "SUPERSEDED"
    QUARANTINED = "QUARANTINED"
    UNKNOWN = "UNKNOWN"


# Legal record-lifecycle transitions (directed graph). A record can always
# be quarantined/rejected from any non-terminal state.
_RECORD_TRANSITIONS: dict[RecordStatus, set[RecordStatus]] = {
    RecordStatus.INGESTED: {RecordStatus.NORMALIZED, RecordStatus.QUARANTINED, RecordStatus.REJECTED},
    RecordStatus.NORMALIZED: {RecordStatus.VALIDATED, RecordStatus.QUARANTINED, RecordStatus.REJECTED},
    RecordStatus.VALIDATED: {
        RecordStatus.RECONCILED,
        RecordStatus.QUARANTINED,
        RecordStatus.REJECTED,
    },
    RecordStatus.RECONCILED: {RecordStatus.CANONICAL, RecordStatus.QUARANTINED},
    RecordStatus.CANONICAL: set(),
    RecordStatus.QUARANTINED: {RecordStatus.VALIDATED, RecordStatus.REJECTED},  # can be re-reviewed
    RecordStatus.REJECTED: set(),
}

# Legal canonical business-state transitions.
_CANONICAL_TRANSITIONS: dict[CanonicalStatus, set[CanonicalStatus]] = {
    CanonicalStatus.UNKNOWN: {
        CanonicalStatus.ACTIVE,
        CanonicalStatus.CLOSED,
        CanonicalStatus.QUARANTINED,
    },
    CanonicalStatus.ACTIVE: {
        CanonicalStatus.CLOSED,
        CanonicalStatus.SUPERSEDED,
        CanonicalStatus.QUARANTINED,
        CanonicalStatus.ACTIVE,  # idempotent re-observation
    },
    CanonicalStatus.CLOSED: {
        CanonicalStatus.ACTIVE,  # reopened / reposted
        CanonicalStatus.SUPERSEDED,
        CanonicalStatus.QUARANTINED,
        CanonicalStatus.CLOSED,  # idempotent re-observation
    },
    CanonicalStatus.SUPERSEDED: {CanonicalStatus.ACTIVE, CanonicalStatus.QUARANTINED},
    CanonicalStatus.QUARANTINED: {CanonicalStatus.ACTIVE, CanonicalStatus.CLOSED},
}


class StatusTransitionError(RuntimeError):
    """Raised when an illegal status transition is attempted."""


@dataclass(frozen=True)
class StatusTransition:
    """One auditable status change."""

    subject_id: str
    axis: str  # "record" or "canonical"
    from_status: str
    to_status: str
    reason: str
    at: str
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject_id": self.subject_id,
            "axis": self.axis,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "reason": self.reason,
            "at": self.at,
            "context": dict(self.context),
        }


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _coerce(value: Any, enum_cls) -> Any:
    if isinstance(value, enum_cls):
        return value
    return enum_cls(str(value))


def can_transition(from_status: Any, to_status: Any, axis: str = "record") -> bool:
    """True if ``from_status -> to_status`` is legal on the given axis."""
    if axis == "record":
        f = _coerce(from_status, RecordStatus)
        t = _coerce(to_status, RecordStatus)
        return t in _RECORD_TRANSITIONS.get(f, set())
    if axis == "canonical":
        f = _coerce(from_status, CanonicalStatus)
        t = _coerce(to_status, CanonicalStatus)
        return t in _CANONICAL_TRANSITIONS.get(f, set())
    raise ValueError(f"Unknown status axis '{axis}' (expected 'record' or 'canonical').")


def apply_transition(
    subject_id: str,
    from_status: Any,
    to_status: Any,
    reason: str,
    axis: str = "record",
    at: Optional[str] = None,
    context: Optional[dict[str, Any]] = None,
) -> StatusTransition:
    """Validate + record a transition. Raises on an illegal transition."""
    if not can_transition(from_status, to_status, axis=axis):
        raise StatusTransitionError(
            f"Illegal {axis} transition {from_status} -> {to_status} for {subject_id!r}."
        )
    return StatusTransition(
        subject_id=subject_id,
        axis=axis,
        from_status=str(getattr(from_status, "value", from_status)),
        to_status=str(getattr(to_status, "value", to_status)),
        reason=reason,
        at=at or _utcnow(),
        context=context or {},
    )
