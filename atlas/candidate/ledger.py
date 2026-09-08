"""Candidate evidence ledger (Phase 1B, build spec section 16/19).

Append-only ledger of :class:`CandidateClaim` rows that PRESERVES conflicts
instead of silently resolving them, and refuses to promote a weaker evidence
class into a stronger one. Candidate-confirmed future facts resolve conflicts
only through an explicit :meth:`resolve` command — never by editing history.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from atlas.candidate.models import (
    CandidateClaim,
    ConflictState,
    EvidenceClass,
    IllegalPromotion,
    can_promote,
)


@dataclass(frozen=True)
class ResolvedClaim:
    topic: str
    scope: str
    value: str
    evidence_class: EvidenceClass
    conflict: bool
    contributing_claim_ids: tuple[str, ...]


class CandidateLedger:
    def __init__(self) -> None:
        self._claims: list[CandidateClaim] = []  # append-only history
        self._resolutions: dict[tuple[str, str], CandidateClaim] = {}

    # -- ingestion ----------------------------------------------------------
    def add(self, claim: CandidateClaim) -> None:
        """Append a claim. History is never mutated; conflicts are detected
        in the resolved view, not by overwriting."""
        self._claims.append(claim)

    def add_many(self, claims: Iterable[CandidateClaim]) -> None:
        for c in claims:
            self.add(c)

    def resolve(
        self,
        topic: str,
        value: str,
        evidence_class: EvidenceClass,
        *,
        scope: str = "",
        claim_id: Optional[str] = None,
        notes: str = "",
    ) -> CandidateClaim:
        """Record an EXPLICIT candidate-confirmed resolution for a (topic,
        scope). This appends a new resolving claim; it does not edit or delete
        any historical claim.

        Candidate confirmation may resolve a VALUE conflict, but it cannot
        establish PROFESSIONAL provenance by itself (build spec 16 / P0-8):
        resolving to PROFESSIONAL requires an appropriate existing documentary
        claim (PROFESSIONAL or PROJECT_PRODUCT) for the same (topic, scope)."""
        if evidence_class == EvidenceClass.PROFESSIONAL:
            has_documentary = any(
                c.topic == topic and c.scope == scope
                and c.evidence_class in (EvidenceClass.PROFESSIONAL, EvidenceClass.PROJECT_PRODUCT)
                for c in self._claims
            )
            if not has_documentary:
                raise IllegalPromotion(
                    f"candidate confirmation alone cannot establish PROFESSIONAL provenance for "
                    f"{topic!r}/{scope!r}; an appropriate documentary/source claim is required"
                )
        resolving = CandidateClaim(
            claim_id=claim_id or f"resolve::{topic}::{scope}::{len(self._claims)}",
            topic=topic,
            normalized_value=value,
            evidence_class=evidence_class,
            source_document_id="candidate_confirmation",
            scope=scope,
            confidence=0.99,
            conflict_state=ConflictState.RESOLVED_BY_CANDIDATE,
            notes=notes,
        )
        self._claims.append(resolving)
        self._resolutions[(topic, scope)] = resolving
        return resolving

    # -- guards -------------------------------------------------------------
    @staticmethod
    def assert_no_illegal_promotion(current: EvidenceClass, target: EvidenceClass) -> None:
        """Guard against upgrading a claim's class beyond its evidence (e.g.
        SKILLS_LIST_ONLY C# -> PROFESSIONAL C#, or CANDIDATE_CONFIRMED
        microservices -> PROFESSIONAL production ownership)."""
        if not can_promote(current, target):
            raise IllegalPromotion(
                f"cannot promote {current.value} -> {target.value} without explicit candidate evidence"
            )

    # -- queries ------------------------------------------------------------
    def claims(self) -> list[CandidateClaim]:
        return list(self._claims)

    def by_class(self, evidence_class: EvidenceClass) -> list[CandidateClaim]:
        return [c for c in self._claims if c.evidence_class == evidence_class]

    def count_by_class(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in self._claims:
            out[c.evidence_class.value] = out.get(c.evidence_class.value, 0) + 1
        return out

    def _topics(self) -> set[tuple[str, str]]:
        return {(c.topic, c.scope) for c in self._claims}

    def resolved(self) -> list[ResolvedClaim]:
        """Compute the resolved view. A (topic, scope) with multiple distinct
        values across sources — and no explicit candidate resolution — is an
        UNRESOLVED conflict and is surfaced as such, never guessed away."""
        out: list[ResolvedClaim] = []
        for topic, scope in sorted(self._topics()):
            group = [c for c in self._claims if c.topic == topic and c.scope == scope]
            resolution = self._resolutions.get((topic, scope))
            if resolution is not None:
                out.append(
                    ResolvedClaim(
                        topic=topic, scope=scope, value=resolution.normalized_value,
                        evidence_class=resolution.evidence_class, conflict=False,
                        contributing_claim_ids=tuple(c.claim_id for c in group),
                    )
                )
                continue
            distinct_values = {c.normalized_value for c in group}
            if len(distinct_values) > 1:
                out.append(
                    ResolvedClaim(
                        topic=topic, scope=scope, value="|".join(sorted(distinct_values)),
                        evidence_class=EvidenceClass.UNRESOLVED_CONFLICT, conflict=True,
                        contributing_claim_ids=tuple(c.claim_id for c in group),
                    )
                )
            else:
                strongest = max(group, key=lambda c: _class_rank(c.evidence_class))
                out.append(
                    ResolvedClaim(
                        topic=topic, scope=scope, value=strongest.normalized_value,
                        evidence_class=strongest.evidence_class, conflict=False,
                        contributing_claim_ids=tuple(c.claim_id for c in group),
                    )
                )
        return out

    def unresolved_conflicts(self) -> list[ResolvedClaim]:
        return [r for r in self.resolved() if r.conflict]

    # -- serialization ------------------------------------------------------
    def to_list(self) -> list[dict]:
        return [c.to_dict() for c in self._claims]

    @classmethod
    def from_list(cls, rows: Iterable[dict]) -> "CandidateLedger":
        ledger = cls()
        for row in rows:
            claim = CandidateClaim.from_dict(row)
            ledger._claims.append(claim)
            if claim.conflict_state == ConflictState.RESOLVED_BY_CANDIDATE:
                ledger._resolutions[(claim.topic, claim.scope)] = claim
        return ledger


def _class_rank(ec: EvidenceClass) -> int:
    from atlas.candidate.models import _STRENGTH

    return _STRENGTH[ec]


__all__ = ["CandidateLedger", "ResolvedClaim"]
