"""Phase 1B — candidate evidence ledger tests (build spec section 16/24).

These use the SYNTHETIC public fixture and the deterministic verified-ledger
builder. Populated real evidence must never be committed (see the privacy
tests in test_phase1b_import_matrix.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas.candidate.importer import build_synthetic_ledger, import_candidate_evidence
from atlas.candidate.ledger import CandidateLedger
from atlas.candidate.models import (
    CandidateClaim,
    ConflictState,
    EvidenceClass,
    IllegalPromotion,
)

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "candidate" / "synthetic_claims.json"


def _synthetic_ledger() -> CandidateLedger:
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))["claims"]
    return CandidateLedger.from_list(rows)


def test_evidence_classes_stay_distinct():
    L = _synthetic_ledger()
    classes = L.count_by_class()
    assert classes.get("PROFESSIONAL", 0) >= 1
    assert classes.get("PROJECT_PRODUCT", 0) >= 1
    assert classes.get("CANDIDATE_CONFIRMED", 0) >= 1
    assert classes.get("SKILLS_LIST_ONLY", 0) >= 1
    # A professional Java claim is not the same object/class as listed Kotlin.
    java = [c for c in L.claims() if c.topic == "Java"][0]
    kotlin = [c for c in L.claims() if c.topic == "Kotlin"][0]
    assert java.evidence_class == EvidenceClass.PROFESSIONAL
    assert kotlin.evidence_class == EvidenceClass.SKILLS_LIST_ONLY


def test_unresolved_conflicts_remain_unresolved():
    L = _synthetic_ledger()
    conflicts = L.unresolved_conflicts()
    topics = {c.topic for c in conflicts}
    assert "cloud production ownership" in topics
    for c in conflicts:
        assert c.evidence_class == EvidenceClass.UNRESOLVED_CONFLICT
        assert c.conflict is True


def test_skills_list_cannot_become_professional():
    L = CandidateLedger()
    with pytest.raises(IllegalPromotion):
        L.assert_no_illegal_promotion(EvidenceClass.SKILLS_LIST_ONLY, EvidenceClass.PROFESSIONAL)


def test_candidate_confirmed_microservices_is_not_production_ownership():
    L = build_synthetic_ledger()
    # candidate-confirmed knowledge must not auto-promote to professional ownership
    with pytest.raises(IllegalPromotion):
        L.assert_no_illegal_promotion(EvidenceClass.CANDIDATE_CONFIRMED, EvidenceClass.PROFESSIONAL)
    # and microservices production ownership is a preserved conflict
    conflict_topics = {c.topic for c in L.unresolved_conflicts()}
    assert "microservices production ownership" in conflict_topics


def test_explicit_resolution_does_not_edit_history():
    L = build_synthetic_ledger()
    before = len(L.claims())
    L.resolve("microservices production ownership", "built 3 services", EvidenceClass.PROFESSIONAL, scope="ownership")
    after = len(L.claims())
    assert after == before + 1  # appended, not overwritten
    # after explicit resolution it is no longer an unresolved conflict
    conflict_topics = {c.topic for c in L.unresolved_conflicts()}
    assert "microservices production ownership" not in conflict_topics


def test_synthetic_ledger_preserves_documented_conflict_structure():
    L = build_synthetic_ledger()
    topics = {c.topic for c in L.unresolved_conflicts()}
    # Conflict STRUCTURE is preserved with generic (PII-free) topic labels.
    assert "current-role title/timeline" in topics
    assert "additional internship period" in topics
    assert "prior-internship dates/title" in topics
    assert "microservices production ownership" in topics


def test_import_writes_only_to_gitignored_private_path(tmp_path):
    out = tmp_path / "private" / "candidate_evidence.private.json"
    result = import_candidate_evidence(source=tmp_path / "missing.md", out_path=out, write=True)
    assert out.exists()
    assert result.claim_count > 0
    # output filename matches a gitignored pattern (*.private.json / private/)
    assert out.name.endswith(".private.json")
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "claims" in data and data["schema_version"] == 1


def test_synthetic_fixture_contains_no_pii():
    text = FIXTURE.read_text(encoding="utf-8").lower()
    # name tokens built by concatenation so this file holds no literal name
    for banned in ("dev" + "ati", "swa" + "rup", "@", "phone"):
        assert banned not in text
