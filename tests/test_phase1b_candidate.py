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
    # Candidate confirmation resolves the VALUE conflict (not as PROFESSIONAL).
    L.resolve("microservices production ownership", "built 3 services",
              EvidenceClass.CANDIDATE_CONFIRMED, scope="ownership")
    after = len(L.claims())
    assert after == before + 1  # appended, not overwritten
    # after explicit resolution it is no longer an unresolved conflict
    conflict_topics = {c.topic for c in L.unresolved_conflicts()}
    assert "microservices production ownership" not in conflict_topics


def test_candidate_confirmation_cannot_establish_professional_provenance():
    # P0-8: candidate confirmation alone cannot make a claim PROFESSIONAL when
    # there is no documentary basis for that topic/scope.
    L = build_synthetic_ledger()
    with pytest.raises(IllegalPromotion):
        L.resolve("microservices production ownership", "built 3 services",
                  EvidenceClass.PROFESSIONAL, scope="ownership")


def test_project_product_and_candidate_confirmed_not_interchangeable():
    # P0-8: same numeric rank does NOT make them interchangeable.
    L = CandidateLedger()
    with pytest.raises(IllegalPromotion):
        L.assert_no_illegal_promotion(EvidenceClass.CANDIDATE_CONFIRMED, EvidenceClass.PROJECT_PRODUCT)
    with pytest.raises(IllegalPromotion):
        L.assert_no_illegal_promotion(EvidenceClass.PROJECT_PRODUCT, EvidenceClass.CANDIDATE_CONFIRMED)
    # A documentary downgrade IS allowed.
    L.assert_no_illegal_promotion(EvidenceClass.PROFESSIONAL, EvidenceClass.PROJECT_PRODUCT)


def test_professional_resolution_allowed_with_documentary_basis():
    L = CandidateLedger()
    L.add(CandidateClaim(claim_id="doc1", topic="Java", normalized_value="prod",
                         evidence_class=EvidenceClass.PROJECT_PRODUCT, source_document_id="resume", scope="x"))
    # With an existing documentary claim, a professional resolution is allowed.
    L.resolve("Java", "professional Java work", EvidenceClass.PROFESSIONAL, scope="x")


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


def test_parser_preserves_parent_class_across_nested_headings():
    # P0-8: a professional-evidence section followed by an employer subheading
    # must NOT reset the class to CANDIDATE_CONFIRMED. Uses SYNTHETIC markdown
    # (no real PII) so it is safe to commit.
    from atlas.candidate.importer import parse_private_profile

    md = """
## Professional Evidence
### Generic Employer A
- Built backend services in Java
- Maintained REST APIs
### Generic Employer B
- Owned a payments module

## Project/Product Evidence
### Personal Project
- Implemented a TypeScript SPA

## Conflicts / Clarifications
- current-role title differs between resume and profile
""".strip()
    ledger = parse_private_profile(md)
    by_topic = {c.topic: c for c in ledger.claims()}
    # Bullets under nested employer subheadings inherit PROFESSIONAL, not the default.
    assert by_topic["Built backend services in Java"].evidence_class == EvidenceClass.PROFESSIONAL
    assert by_topic["Owned a payments module"].evidence_class == EvidenceClass.PROFESSIONAL
    assert by_topic["Implemented a TypeScript SPA"].evidence_class == EvidenceClass.PROJECT_PRODUCT
    conflict_claim = by_topic["current-role title differs between resume and profile"]
    assert conflict_claim.evidence_class == EvidenceClass.UNRESOLVED_CONFLICT
    assert conflict_claim.conflict_state == ConflictState.UNRESOLVED
    # Stable claim identity carries the source document hash.
    assert all("parsed::" in c.claim_id for c in ledger.claims())


def test_parser_quarantines_long_bullets_without_dropping():
    from atlas.candidate.importer import parse_private_profile

    long_bullet = "x " * 300  # > 200 chars
    md = f"## Professional Evidence\n- {long_bullet}"
    ledger = parse_private_profile(md)
    assert len(ledger.claims()) == 1  # NOT silently dropped
    claim = ledger.claims()[0]
    assert claim.notes.startswith("TRUNCATED_FROM_")
    assert len(claim.normalized_value) <= 500
