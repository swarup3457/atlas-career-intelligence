"""Redacted, PII-free candidate SEARCH profile for the LLM India pilot (audit 3.6).

The V2 live hunt hardcoded a synthetic 2.0-year candidate. This module loads the REAL
private verified profile through the approved
:func:`atlas.candidate.importer.import_candidate_evidence` importer (which writes the
full, real ledger ONLY to the gitignored ``config/private`` store) and derives a compact,
**PII-free** :class:`CandidateSearchProfile` containing only what the company-search agent
and deterministic matcher need: target lanes, evidence-classed *technology* skills, an
experience band, India location preferences, work-mode and eligibility preferences.

Name, phone, email, addresses, employer names and dates NEVER enter this profile or any
engineering evidence. In *live* mode a missing/ambiguous private profile is
``WAITING_FOR_HUMAN`` — the synthetic ledger is never silently substituted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from atlas.candidate.importer import import_candidate_evidence
from atlas.candidate.ledger import CandidateLedger
from atlas.candidate.models import EvidenceClass, _STRENGTH
from atlas.data_integrity.normalizers import identity_token
from atlas.hunt.matching import HuntCandidate

__all__ = [
    "CandidateSearchProfile",
    "CandidateProfileError",
    "CandidateProfileWaitingForHuman",
    "load_candidate_search_profile",
    "PRIMARY_LANES",
]

PRIMARY_LANES = (
    "JAVA_BACKEND",
    "JAVA_FULLSTACK",
    "REACT_FRONTEND",
    "DOTNET",
    "ENTERPRISE_HR_PAYROLL_INTEGRATION",
)

# Recognized technology / domain vocabulary. Only these tokens may appear as skills in the
# redacted profile — anything else in the private ledger (names, employers, dates, free
# text) is dropped, so no PII can leak. canonical -> match aliases.
_SKILL_VOCAB: dict[str, tuple[str, ...]] = {
    "Java": ("java", "jvm"),
    "Spring Boot": ("spring boot", "spring"),
    "Spring MVC": ("spring mvc", "spring boot mvc"),
    "Spring Data JPA": ("spring data jpa", "jpa"),
    "Hibernate": ("hibernate",),
    "Jakarta EE": ("jakarta", "j2ee", "javaee"),
    "REST APIs": ("restful apis", "rest apis", "rest", "restful"),
    "Microservices": ("microservices",),
    "MySQL": ("mysql",),
    "SQL": ("sql",),
    "Maven": ("maven",),
    "JWT": ("jwt",),
    "React": ("react", "reactjs", "react.js"),
    "React Router": ("react router",),
    "React Hook Form": ("react hook form",),
    "Axios": ("axios",),
    "JavaScript": ("javascript",),
    "TypeScript": ("typescript",),
    "HTML": ("html",),
    "CSS": ("css",),
    "Git": ("git",),
    "C#": ("c#",),
    "ASP.NET": ("asp.net", "asp .net"),
    ".NET": (".net", "dotnet"),
    "Payroll": ("payroll",),
    "HR technology": ("hr software", "hr technology", "human resources", "hcm", "hris"),
    "CI/CD": ("ci/cd", "cicd"),
    "Unit testing": ("unit and integration testing", "unit testing", "integration testing"),
}

# Bullets whose leading label carries a non-PII preference we DO want.
_YEARS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:[-–]\s*(\d+(?:\.\d+)?))?\s*\+?\s*years?", re.I)


@dataclass(frozen=True)
class CandidateSearchProfile:
    total_experience_years: float
    experience_band: str
    target_lanes: tuple[str, ...]
    location_group: str
    preferred_locations: tuple[str, ...]
    work_mode_preference: str
    india_based: bool
    requires_sponsorship: bool
    skills: Mapping[str, str]  # canonical skill -> EvidenceClass value (redacted, tech only)
    provenance_sha256: str = ""
    source_id: str = ""
    synthetic: bool = False
    clarifications_pending: tuple[str, ...] = ()

    @property
    def gate_mode(self) -> str:
        """Canonical candidate-gate mode string for evidence/machine-gate reporting.

        The pilot loads the real PRIVATE, LOCAL candidate profile (config/private via the
        approved importer), so a real profile reports ``PRIVATE_LOCAL``. A synthetic
        fallback (offline tests only) reports ``SYNTHETIC``.
        """
        return "SYNTHETIC" if self.synthetic else "PRIVATE_LOCAL"

    def to_hunt_candidate(self) -> HuntCandidate:
        evidence = {
            identity_token(skill): EvidenceClass(cls)
            for skill, cls in self.skills.items()
            if identity_token(skill)
        }
        return HuntCandidate(
            total_experience_years=self.total_experience_years,
            target_lanes=tuple(self.target_lanes),
            location_group=self.location_group,
            evidence=evidence,
            strong_overall=not self.synthetic,
        )

    def redacted_dict(self) -> dict:
        """Compact, PII-free view safe to hand to the runtime LLM and to write to evidence."""
        return {
            "experience_years": self.total_experience_years,
            "experience_band": self.experience_band,
            "target_lanes": list(self.target_lanes),
            "location_group": self.location_group,
            "preferred_locations": list(self.preferred_locations),
            "work_mode_preference": self.work_mode_preference,
            "india_based": self.india_based,
            "requires_sponsorship": self.requires_sponsorship,
            "skills": {k: v for k, v in sorted(self.skills.items())},
            "clarifications_pending": list(self.clarifications_pending),
            "provenance": {"source_sha256": self.provenance_sha256[:16], "synthetic": self.synthetic},
        }


class CandidateProfileError(RuntimeError):
    pass


class CandidateProfileWaitingForHuman(CandidateProfileError):
    """The private profile is missing/ambiguous; live mode must not use synthetic data."""


def _strength(cls: EvidenceClass) -> int:
    return _STRENGTH.get(cls, 0)


def _extract_skills(ledger: CandidateLedger) -> dict[str, str]:
    best: dict[str, EvidenceClass] = {}
    for claim in ledger.claims():
        text = f"{claim.topic} {claim.normalized_value}".lower()
        for canonical, aliases in _SKILL_VOCAB.items():
            for alias in aliases:
                if re.search(r"(?<![a-z0-9+.#])" + re.escape(alias) + r"(?![a-z0-9+#])", text):
                    cur = best.get(canonical)
                    if cur is None or _strength(claim.evidence_class) > _strength(cur):
                        best[canonical] = claim.evidence_class
                    break
    return {k: v.value for k, v in best.items()}


def _derive_lanes(skills: Mapping[str, str]) -> tuple[str, ...]:
    have = set(skills)
    lanes: list[str] = []
    if "Java" in have or "Spring Boot" in have:
        lanes.append("JAVA_BACKEND")
        if have & {"React", "JavaScript", "TypeScript"}:
            lanes.append("JAVA_FULLSTACK")
    if have & {"React"}:
        lanes.append("REACT_FRONTEND")
    if have & {"C#", "ASP.NET", ".NET"}:
        lanes.append("DOTNET")
    if have & {"Payroll", "HR technology"}:
        lanes.append("ENTERPRISE_HR_PAYROLL_INTEGRATION")
    # de-dup, preserve pilot order
    return tuple(l for l in PRIMARY_LANES if l in lanes)


def _extract_experience(ledger: CandidateLedger) -> tuple[float, str]:
    for claim in ledger.claims():
        text = f"{claim.topic} {claim.normalized_value}".lower()
        if "experience range" in text or "target experience" in text:
            nums = _YEARS_RE.findall(text)
            if nums:
                lo = float(nums[0][0])
                hi = float(nums[0][1]) if nums[0][1] else lo
                band = f"{lo:g}-{hi:g}" if hi != lo else f"{lo:g}"
                # candidate's usable professional years ~ upper suitable band (2+)
                years = 2.0 if lo <= 2 <= max(hi, lo) or "2+" in text else max(lo, 2.0)
                return years, band
    return 2.0, "1-3"


def _extract_locations(ledger: CandidateLedger) -> tuple[tuple[str, ...], str]:
    cities: list[str] = []
    for claim in ledger.claims():
        text = f"{claim.topic} {claim.normalized_value}".lower()
        if "location preference" in text or "primary location" in text:
            for city, group in (("Bengaluru", "PRIMARY"), ("Bangalore", "PRIMARY"),
                                ("Hyderabad", "PRIMARY"), ("Pune", "SECONDARY"),
                                ("Chennai", "SECONDARY"), ("Mumbai", "SECONDARY"),
                                ("Noida", "SECONDARY"), ("Gurugram", "SECONDARY")):
                if city.lower() in text and city not in cities:
                    cities.append(city)
    if not cities:
        cities = ["Bengaluru", "Hyderabad"]
    return tuple(cities), "PRIMARY"


def load_candidate_search_profile(
    *,
    live: bool = True,
    allow_synthetic: bool = False,
    source: Optional[Path] = None,
    private_out: Optional[Path] = None,
    persist: bool = False,
) -> CandidateSearchProfile:
    """Load the redacted candidate search profile.

    In live mode (``live=True``) a missing/ambiguous private profile raises
    :class:`CandidateProfileWaitingForHuman` instead of substituting synthetic data,
    unless ``allow_synthetic`` is explicitly set (offline tests only).

    The real (PII-bearing) ledger is only persisted to the gitignored ``config/private``
    store when ``persist=True``; by default nothing is written to disk — the redacted,
    PII-free profile is derived entirely in memory.
    """
    result = import_candidate_evidence(source=source, out_path=private_out, write=persist)
    if result.used_synthetic and live and not allow_synthetic:
        raise CandidateProfileWaitingForHuman(
            "private candidate search profile is missing or ambiguous; live LLM pilot requires "
            "the approved private profile (config/private). Refusing to run on synthetic data."
        )

    ledger = result.ledger
    skills = _extract_skills(ledger)
    lanes = _derive_lanes(skills) or PRIMARY_LANES
    years, band = _extract_experience(ledger)
    locations, group = _extract_locations(ledger)
    # Curated, PII-free clarification topics (never raw bullets, which can carry
    # employer names / dates). These mirror the verified profile's open items.
    clarifications = (
        "notice period",
        "preferred work mode",
        "relocation flexibility",
        "work authorization / sponsorship",
        "microservices production ownership",
        "unit/integration testing frameworks",
        "CI/CD products",
        "cloud exposure",
        "C# professional depth",
    )

    return CandidateSearchProfile(
        total_experience_years=years,
        experience_band=band,
        target_lanes=lanes,
        location_group=group,
        preferred_locations=locations,
        work_mode_preference="ANY",  # profile: preferred work mode is a pending clarification
        india_based=True,
        requires_sponsorship=False,  # India-domestic roles need no sponsorship
        skills=skills,
        provenance_sha256=result.source_sha256,
        source_id="06_CANDIDATE_PROFILE_VERIFIED.md",
        synthetic=result.used_synthetic,
        clarifications_pending=clarifications,
    )
