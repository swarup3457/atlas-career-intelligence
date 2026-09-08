"""Private candidate evidence importer (Phase 1B, build spec section 16).

PRIVACY-CRITICAL: this committed module contains only the SCHEMA, a generic
parser, and a SYNTHETIC ledger. It never hardcodes the real candidate's
populated evidence (employers, timelines, personal identifiers).

- :func:`build_synthetic_ledger` — a synthetic, PII-free ledger (generic
  topics only) used by demos/tests and as a fallback. It preserves the
  *structure* of the documented conflicts without any real employer names.
- :func:`parse_private_profile` — parses the private verified-profile
  markdown into claims. Its output contains real candidate data and is
  written ONLY to a gitignored local path.
- :func:`import_candidate_evidence` — runs the import into the gitignored
  private store; falls back to the synthetic ledger when the private source
  is absent.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from atlas.candidate.ledger import CandidateLedger
from atlas.candidate.models import CandidateClaim, ConflictState, EvidenceClass

# Default read-only private source (outside the repo) and gitignored output.
DEFAULT_SOURCE = Path(r"C:\Atlas-Agent-Import\06_CANDIDATE_PROFILE_VERIFIED.md")
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PRIVATE_OUT = REPO_ROOT / "config" / "private" / "candidate_evidence.private.json"


@dataclass(frozen=True)
class ImportResult:
    ledger: CandidateLedger
    source_sha256: str
    out_path: Optional[str]
    claim_count: int
    used_synthetic: bool


def _c(claim_id, topic, value, ec, source, *, scope="", conflict=ConflictState.NONE, notes="", confidence=0.7):
    return CandidateClaim(
        claim_id=claim_id, topic=topic, normalized_value=value, evidence_class=ec,
        source_document_id=source, scope=scope, conflict_state=conflict, notes=notes, confidence=confidence,
    )


def build_synthetic_ledger() -> CandidateLedger:
    """A synthetic, PII-free ledger. Topics are generic technology/evidence
    labels; conflict *structure* mirrors the real profile (a current-role
    title/timeline conflict, a prior-internship conflict, an additional
    internship period, and a microservices production-ownership conflict) but
    uses NO real employer names or identifiers."""
    L = CandidateLedger()
    src, profile = "synthetic_resume", "synthetic_profile"

    for tech in ("Java", "Spring Boot", "RESTful APIs", "React", "JavaScript", "MySQL", "Hibernate", "Git"):
        L.add(_c(f"prof::{tech}", tech, "professional", EvidenceClass.PROFESSIONAL, src, scope="employment", confidence=0.85))
    L.add(_c("prof::payroll_hr", "enterprise payroll and HR software", "professional",
             EvidenceClass.PROFESSIONAL, src, scope="domain", confidence=0.8))

    for tech in ("ASP.NET", "Spring Data JPA", "JWT", "TypeScript", "Maven", "React Router", "Axios"):
        L.add(_c(f"proj::{tech}", tech, "project_product", EvidenceClass.PROJECT_PRODUCT, src, scope="project", confidence=0.7))

    L.add(_c("cc::microservices", "microservices", "knowledge", EvidenceClass.CANDIDATE_CONFIRMED,
             profile, scope="knowledge", notes="knows Java/Spring Boot/microservices", confidence=0.6))
    L.add(_c("skill::csharp", "C#", "listed", EvidenceClass.SKILLS_LIST_ONLY, src, scope="skills_section",
             notes="C# listed in resume Skills section only", confidence=0.4))

    # Preserved UNRESOLVED conflicts — generic topics, no real employer names.
    L.add(_c("conflict::ms_ownership::a", "microservices production ownership", "unresolved",
             EvidenceClass.CANDIDATE_CONFIRMED, profile, scope="ownership", confidence=0.3))
    L.add(_c("conflict::ms_ownership::b", "microservices production ownership", "not_established",
             EvidenceClass.UNSUPPORTED, src, scope="ownership", confidence=0.3))
    L.add(_c("conflict::current_role::a", "current-role title/timeline", "developer recent-present",
             EvidenceClass.PROFESSIONAL, src, scope="current_role", confidence=0.6))
    L.add(_c("conflict::current_role::b", "current-role title/timeline", "representation differs",
             EvidenceClass.CANDIDATE_CONFIRMED, profile, scope="current_role", confidence=0.5))
    L.add(_c("conflict::extra_internship::a", "additional internship period", "candidate_stated",
             EvidenceClass.CANDIDATE_CONFIRMED, profile, scope="internship", confidence=0.4))
    L.add(_c("conflict::extra_internship::b", "additional internship period", "not_in_resume",
             EvidenceClass.UNSUPPORTED, src, scope="internship", confidence=0.4))
    L.add(_c("conflict::prior_internship::a", "prior-internship dates/title", "intern_period",
             EvidenceClass.PROJECT_PRODUCT, src, scope="prior_internship", confidence=0.5))
    L.add(_c("conflict::prior_internship::b", "prior-internship dates/title", "wording differs",
             EvidenceClass.CANDIDATE_CONFIRMED, profile, scope="prior_internship", confidence=0.4))
    return L


_SECTION_CLASS = (
    (re.compile(r"professional evidence", re.I), EvidenceClass.PROFESSIONAL),
    (re.compile(r"project/product|project evidence", re.I), EvidenceClass.PROJECT_PRODUCT),
    (re.compile(r"skills? list|skills section|\.NET and frontend", re.I), EvidenceClass.SKILLS_LIST_ONLY),
    (re.compile(r"microservices|candidate.?confirmed", re.I), EvidenceClass.CANDIDATE_CONFIRMED),
    (re.compile(r"conflict|clarification", re.I), EvidenceClass.UNRESOLVED_CONFLICT),
)


def _class_for_section(header: str) -> EvidenceClass:
    for pattern, ec in _SECTION_CLASS:
        if pattern.search(header):
            return ec
    return EvidenceClass.CANDIDATE_CONFIRMED


def parse_private_profile(text: str) -> CandidateLedger:
    """Parse the private verified-profile markdown into a ledger. The output
    contains REAL candidate data and must only be written to gitignored
    storage. Deterministic, header-driven extraction."""
    ledger = CandidateLedger()
    current_class = EvidenceClass.CANDIDATE_CONFIRMED
    idx = 0
    for line in text.splitlines():
        header = re.match(r"\s{0,3}#{2,4}\s+(.*)", line)
        if header:
            current_class = _class_for_section(header.group(1))
            continue
        bullet = re.match(r"\s*[-*]\s+(.+)", line)
        if bullet:
            topic = bullet.group(1).strip()
            if not topic or len(topic) > 120:
                continue
            idx += 1
            ledger.add(
                CandidateClaim(
                    claim_id=f"parsed::{idx}", topic=topic[:80], normalized_value=topic[:120],
                    evidence_class=current_class, source_document_id="private_profile",
                    scope="parsed", confidence=0.5,
                    conflict_state=(
                        ConflictState.UNRESOLVED
                        if current_class == EvidenceClass.UNRESOLVED_CONFLICT
                        else ConflictState.NONE
                    ),
                )
            )
    return ledger


def import_candidate_evidence(
    source: Optional[Path] = None,
    out_path: Optional[Path] = None,
    *,
    write: bool = True,
) -> ImportResult:
    """Import candidate evidence into a private ledger. When the private source
    exists, its real content is parsed and written ONLY to the gitignored
    ``out_path``. When absent, the synthetic (PII-free) ledger is used."""
    source = Path(source) if source else DEFAULT_SOURCE
    out_path = Path(out_path) if out_path is not None else DEFAULT_PRIVATE_OUT

    if source.exists():
        raw = source.read_text(encoding="utf-8", errors="ignore")
        source_sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        ledger = parse_private_profile(raw)
        used_synthetic = len(ledger.claims()) == 0
        if used_synthetic:
            ledger = build_synthetic_ledger()
    else:
        source_sha = ""
        ledger = build_synthetic_ledger()
        used_synthetic = True

    written: Optional[str] = None
    if write:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "provenance": {"source_sha256": source_sha, "source_id": "06_CANDIDATE_PROFILE_VERIFIED.md",
                           "synthetic": used_synthetic},
            "claims": ledger.to_list(),
        }
        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        written = str(out_path)

    return ImportResult(
        ledger=ledger, source_sha256=source_sha, out_path=written,
        claim_count=len(ledger.claims()), used_synthetic=used_synthetic,
    )


__all__ = [
    "ImportResult",
    "build_synthetic_ledger",
    "parse_private_profile",
    "import_candidate_evidence",
    "DEFAULT_SOURCE",
    "DEFAULT_PRIVATE_OUT",
]
