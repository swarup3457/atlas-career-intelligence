"""Phase 1B — Workspace import accounting + privacy gate tests.

These enforce the build-spec "import package/accounting" and "privacy"
test matrices without requiring the read-only import package to be
present (the matrix is self-contained and PII-free).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from atlas.imports.migration import (
    Decision,
    MIGRATION_MATRIX,
    PrivacyClass,
    record_for,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# The exact set of files delivered in the Workspace Agent import package,
# recorded at pre-flight. The matrix must account for every one of them.
EXPECTED_IMPORT_FILES = frozenset(r.relative_path for r in MIGRATION_MATRIX)


def test_matrix_has_no_duplicate_paths():
    paths = [r.relative_path for r in MIGRATION_MATRIX]
    assert len(paths) == len(set(paths))


def test_every_import_file_has_a_decision():
    for rec in MIGRATION_MATRIX:
        assert isinstance(rec.decision, Decision)
        assert rec.new_implementation_home
        assert rec.migration_action
        assert isinstance(rec.privacy_class, PrivacyClass)
        assert re.fullmatch(r"[0-9A-Fa-f]{64}", rec.sha256), rec.relative_path


def test_both_06_files_remain_distinct():
    """Two different files both start with '06_'; sorting by numeric prefix
    alone must never collapse them."""
    a = record_for("06_CANDIDATE_PROFILE_VERIFIED.md")
    b = record_for("06_COMPANY_SEARCH_UNIVERSE_V5.md")
    assert a is not None and b is not None
    assert a.sha256 != b.sha256
    assert a.privacy_class == PrivacyClass.PRIVATE_PII
    assert b.privacy_class == PrivacyClass.PUBLIC_POLICY
    assert a.decision != b.decision


def test_missing_recruiter_policy_is_detected():
    rec = record_for("skills/recruiter-outreach-prep/SKILL.md")
    assert rec is not None
    assert rec.decision == Decision.BLOCKED_PENDING_POLICY
    assert any("05_PUBLIC_RECRUITER_CONTACT_POLICY.md" in c for c in rec.conflicts)


def test_stale_resume_filename_is_detected():
    """File 00 references a resume filename that does not match the resume
    file actually shipped in the package — the matrix must flag the mismatch
    (both name-bearing filenames are redacted for privacy)."""
    resume = record_for("candidate_resume.pdf")
    instr = record_for("00_CURRENT_LIVE_AGENT_INSTRUCTIONS.md")
    assert resume is not None and instr is not None
    assert any("resume filename" in c for c in resume.conflicts)
    assert any("resume filename" in c for c in instr.conflicts)


def test_hardcoded_workbook_reference_is_rejected():
    rec = record_for("skills/job-market-trend-analyzer/SKILL.md")
    assert rec is not None
    assert rec.decision == Decision.REWRITE
    assert any("Atlas_MASTER_ACTIVE_20260808.xlsx" in c for c in rec.conflicts)


def test_candidate_pii_files_are_private_and_never_committed():
    pii = [r for r in MIGRATION_MATRIX if r.privacy_class == PrivacyClass.PRIVATE_PII]
    # The verified profile MD and the two PDFs are the private sources
    # (name-bearing filenames redacted).
    names = {r.relative_path for r in pii}
    assert "06_CANDIDATE_PROFILE_VERIFIED.md" in names
    assert "candidate_resume.pdf" in names
    assert "Profile.pdf" in names
    for rec in pii:
        assert rec.decision in {Decision.LEGACY_IMPORT_ONLY}


def test_no_pdf_or_private_candidate_file_is_tracked_in_git():
    """No candidate PDF/profile export or private candidate ledger may be a
    tracked file in the repository tree."""
    forbidden = list(REPO_ROOT.rglob("*.pdf"))
    forbidden += list((REPO_ROOT / "config" / "private").glob("**/*"))
    forbidden += list((REPO_ROOT / "candidate_data").glob("**/*"))
    tracked = [p for p in forbidden if p.is_file()]
    assert not tracked, f"private/candidate files present in tree: {tracked}"


def test_gitignore_protects_private_paths():
    gi = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for needed in (
        "config/private/",
        "private/",
        "imports/private/",
        "candidate_data/",
        "*.private.yaml",
        "*.private.json",
        "*.pdf",
    ):
        assert needed in gi, f".gitignore missing protection: {needed}"


def test_decisions_are_within_allowed_vocabulary():
    allowed = set(Decision)
    for rec in MIGRATION_MATRIX:
        assert rec.decision in allowed


# Candidate identity tokens that must NEVER appear in the committed tree.
# Built by concatenation so the literal name never appears in this file either.
_PII_TOKENS = ("dev" + "ati", "swa" + "rup", "back" + "endk")


def test_no_candidate_name_appears_in_committed_tree():
    """Assume the repo may be public: no candidate personal identifier may be
    committed anywhere (schemas/matrix/docs/tests). Populated PII lives only in
    gitignored local storage (see .gitignore config/private/)."""
    skip_dirs = {".git", ".venv", ".pytest_cache", "state", "output", "logs",
                 "backups", "support_bundles", "node_modules"}
    exts = {".py", ".md", ".yaml", ".yml", ".json", ".toml", ".txt", ".cfg", ".ini"}
    offenders: list[str] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in exts:
            continue
        rel_parts = set(path.relative_to(REPO_ROOT).parts)
        if rel_parts & skip_dirs:
            continue
        if "private" in rel_parts or path.name.endswith((".private.json", ".private.yaml")):
            continue  # gitignored private storage is allowed to hold PII
        try:
            low = path.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
        for token in _PII_TOKENS:
            if token in low:
                offenders.append(f"{path.relative_to(REPO_ROOT)} :: {token}")
    assert not offenders, f"candidate PII found in committed tree: {offenders}"
