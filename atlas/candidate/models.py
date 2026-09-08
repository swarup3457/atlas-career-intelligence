"""Candidate evidence ledger models (Phase 1B, build spec section 16).

Candidate evidence is PRIVATE. The public tree contains only these schemas
and synthetic fixtures — never the real resume, profile export, PII, or
populated evidence rows (those live in a gitignored local store, see
:mod:`atlas.candidate.importer`).

The ledger PRESERVES unresolved differences rather than guessing, and never
promotes a weaker evidence class into a stronger one (e.g. candidate-
confirmed microservices knowledge must never become professional production
ownership; a skills-list C# entry must never become professional C#).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


class EvidenceClass(str, enum.Enum):
    PROFESSIONAL = "PROFESSIONAL"
    PROJECT_PRODUCT = "PROJECT_PRODUCT"
    CANDIDATE_CONFIRMED = "CANDIDATE_CONFIRMED"
    SKILLS_LIST_ONLY = "SKILLS_LIST_ONLY"
    UNRESOLVED_CONFLICT = "UNRESOLVED_CONFLICT"
    UNSUPPORTED = "UNSUPPORTED"


# Strength ordering (higher = stronger), used for tie-break display only —
# NOT for deciding legal transitions (P0-8: PROJECT_PRODUCT and
# CANDIDATE_CONFIRMED share a rank but are NOT interchangeable).
_STRENGTH: dict[EvidenceClass, int] = {
    EvidenceClass.UNSUPPORTED: 0,
    EvidenceClass.SKILLS_LIST_ONLY: 1,
    EvidenceClass.CANDIDATE_CONFIRMED: 2,
    EvidenceClass.PROJECT_PRODUCT: 2,
    EvidenceClass.PROFESSIONAL: 3,
    EvidenceClass.UNRESOLVED_CONFLICT: 0,
}

# EXPLICIT allowed automatic-transition matrix (build spec 16 / P0-8). A claim
# may be replaced automatically ONLY by a target in its allowed set. Cross-
# lineage moves — e.g. CANDIDATE_CONFIRMED (self-asserted) <-> PROJECT_PRODUCT
# (documentary), or anything -> PROFESSIONAL — are NEVER automatic even when
# ranks match; they require an explicit, appropriately-sourced resolution.
_ALLOWED_TRANSITIONS: dict[EvidenceClass, frozenset[EvidenceClass]] = {
    EvidenceClass.PROFESSIONAL: frozenset(
        {EvidenceClass.PROFESSIONAL, EvidenceClass.PROJECT_PRODUCT,
         EvidenceClass.SKILLS_LIST_ONLY, EvidenceClass.UNSUPPORTED}
    ),
    EvidenceClass.PROJECT_PRODUCT: frozenset(
        {EvidenceClass.PROJECT_PRODUCT, EvidenceClass.SKILLS_LIST_ONLY, EvidenceClass.UNSUPPORTED}
    ),
    EvidenceClass.CANDIDATE_CONFIRMED: frozenset(
        {EvidenceClass.CANDIDATE_CONFIRMED, EvidenceClass.UNSUPPORTED}
    ),
    EvidenceClass.SKILLS_LIST_ONLY: frozenset(
        {EvidenceClass.SKILLS_LIST_ONLY, EvidenceClass.UNSUPPORTED}
    ),
    EvidenceClass.UNSUPPORTED: frozenset({EvidenceClass.UNSUPPORTED}),
    EvidenceClass.UNRESOLVED_CONFLICT: frozenset(
        {EvidenceClass.UNRESOLVED_CONFLICT, EvidenceClass.UNSUPPORTED}
    ),
}


class ConflictState(str, enum.Enum):
    NONE = "NONE"
    UNRESOLVED = "UNRESOLVED"
    RESOLVED_BY_CANDIDATE = "RESOLVED_BY_CANDIDATE"


class IllegalPromotion(ValueError):
    """Raised when code attempts to upgrade a claim's evidence class beyond
    what the evidence supports (e.g. SKILLS_LIST_ONLY -> PROFESSIONAL)."""


@dataclass(frozen=True)
class CandidateClaim:
    claim_id: str
    topic: str                      # technology / domain / fact
    normalized_value: str
    evidence_class: EvidenceClass
    source_document_id: str         # logical id, NOT a filename
    scope: str = ""
    source_date: Optional[str] = None
    confidence: float = 0.5
    conflict_state: ConflictState = ConflictState.NONE
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "topic": self.topic,
            "normalized_value": self.normalized_value,
            "evidence_class": self.evidence_class.value,
            "source_document_id": self.source_document_id,
            "scope": self.scope,
            "source_date": self.source_date,
            "confidence": self.confidence,
            "conflict_state": self.conflict_state.value,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CandidateClaim":
        return cls(
            claim_id=str(raw["claim_id"]),
            topic=str(raw["topic"]),
            normalized_value=str(raw["normalized_value"]),
            evidence_class=EvidenceClass(raw["evidence_class"]),
            source_document_id=str(raw["source_document_id"]),
            scope=str(raw.get("scope", "")),
            source_date=raw.get("source_date"),
            confidence=float(raw.get("confidence", 0.5)),
            conflict_state=ConflictState(raw.get("conflict_state", "NONE")),
            notes=str(raw.get("notes", "")),
        )


def can_promote(current: EvidenceClass, target: EvidenceClass) -> bool:
    """Whether ``current`` may be replaced by ``target`` WITHOUT an explicit
    candidate resolution, per the explicit allowed-transition matrix. Same-rank
    but different-lineage classes (PROJECT_PRODUCT vs CANDIDATE_CONFIRMED) are
    NOT interchangeable, and nothing auto-promotes to PROFESSIONAL (P0-8)."""
    return target in _ALLOWED_TRANSITIONS.get(current, frozenset({current}))


__all__ = [
    "EvidenceClass",
    "ConflictState",
    "IllegalPromotion",
    "CandidateClaim",
    "can_promote",
]
