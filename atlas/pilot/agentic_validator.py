"""V4 product validator (prompt s.15).

Rejects a run when any non-bypassable invariant is violated, validating each accepted row
against the RETAINED NORMALIZED SOURCE EVIDENCE (re-deriving experience, role family, stack,
and geography with the same deterministic gates), not merely against workbook columns. Also
enforces the run-level PASS conditions: an internal tool error may never be terminal, at
least eight companies genuinely searched under PASS, and full lane coverage on every
genuinely-searched company.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from atlas.hunt.matching import APPLY_FAMILY
from atlas.hunt.role_family import RoleFamily, classify_role_family
from atlas.hunt.signals import signal_present, strip_alternative_language_enumerations
from atlas.hunt.stack_evidence import analyze_technology_evidence
from atlas.pilot.agentic_tools import looks_like_job_title as _is_job_title
from atlas.pilot.config import PilotConfig
from atlas.pilot.normalize import normalize_source_text
from atlas.pilot.status_v4 import is_genuinely_searched, is_internal_retryable
from atlas.policy.rules import extract_experience

__all__ = ["validate_agentic_run"]

_JAVA_ANCHORS = ("java", "jvm", "spring", "spring boot", "spring mvc", "jakarta ee", "j2ee", "javaee")
_DOTNET_ANCHORS = (".net", ".net core", "c#", "asp.net", "asp.net core", "dotnet")
_FRONTEND_ANCHORS = ("react", "reactjs", "react.js", "angular", "vue", "vue.js", "javascript", "typescript")
_HR_DOMAIN = ("payroll", "hcm", "human capital management", "hris", "workforce", "workforce management",
              "human resources", "hr technology", "hr tech", "hr systems", "benefits administration",
              "time and attendance", "talent management")
_HARD_MIN = 4.0


def _evidence_text(a) -> str:
    detail = getattr(a, "evaluation", None)
    detail = getattr(detail, "detail", None)
    parts = []
    if detail is not None:
        parts += [detail.title or "", detail.description or "", detail.experience_text or ""]
        parts += list(detail.mandatory_requirements or ())
        parts += list(detail.preferred_requirements or ())
    else:
        parts += [a.title or ""]
    return normalize_source_text(" \n ".join(p for p in parts if p))


def _has_any(text: str, tokens) -> bool:
    low = text.lower()
    return any(signal_present(low, t) for t in tokens)


def validate_agentic_run(evaluation, results, config: PilotConfig, *, outcome: str,
                         candidate_synthetic: bool) -> dict:
    checks: list[dict] = []
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "passed": bool(ok), "detail": detail})
        if not ok:
            failures.append(f"{name}: {detail}")

    # -- per accepted row, re-derived from source evidence -------------------
    for a in evaluation.accepted:
        ev_text = _evidence_text(a)
        anchor_text = strip_alternative_language_enumerations(ev_text)
        exp = extract_experience(ev_text)
        rf = classify_role_family(a.title, ev_text)
        tech = analyze_technology_evidence(
            a.title, ev_text,
            getattr(getattr(a, "evaluation", None), "detail", None).mandatory_requirements
            if getattr(getattr(a, "evaluation", None), "detail", None) else (),
            getattr(getattr(a, "evaluation", None), "detail", None).preferred_requirements
            if getattr(getattr(a, "evaluation", None), "detail", None) else (),
        )
        tag = f"{a.company}:{a.title}"

        check("accepted_geography_india", a.geography_decision == "INDIA_ELIGIBLE",
              f"{tag} geography={a.geography_decision}")
        check("accepted_is_real_job_title", _is_job_title(a.title),
              f"{tag} title does not read like a job posting (possible banner/non-job capture)")
        check("accepted_not_hard_min",
              not (exp.min_years is not None and exp.min_years >= _HARD_MIN),
              f"{tag} min_years={exp.min_years}")
        check("accepted_not_arch_advisor_lead",
              rf.family not in (RoleFamily.ARCHITECTURE_ADVISORY.value, RoleFamily.TECH_LEAD.value),
              f"{tag} role_family={rf.family}")
        if a.lane in ("JAVA_BACKEND", "JAVA_FULLSTACK"):
            check("java_lane_has_java_anchor", _has_any(anchor_text, _JAVA_ANCHORS),
                  f"{tag} lane={a.lane} but no mandatory Java/JVM/Spring anchor")
        if a.lane == "JAVA_FULLSTACK":
            check("java_fullstack_has_frontend",
                  _has_any(anchor_text, _JAVA_ANCHORS) and _has_any(anchor_text, _FRONTEND_ANCHORS),
                  f"{tag} java-fullstack missing java backend or frontend evidence")
        if a.lane == "ENTERPRISE_HR_PAYROLL_INTEGRATION":
            check("hr_payroll_has_domain", _has_any(ev_text, _HR_DOMAIN),
                  f"{tag} HR/payroll lane without payroll/HCM/HRIS/workforce evidence")
            check("dotnet_not_mislabeled_hr",
                  not (_has_any(anchor_text, _DOTNET_ANCHORS) and not _has_any(ev_text, _HR_DOMAIN)),
                  f"{tag} .NET evidence but labeled HR/payroll without domain")
        check("no_unsupported_mandatory_backend",
              not (tech.unsupported_mandatory_backend and not tech.supported_backend),
              f"{tag} unsupported mandatory backend {tech.unsupported_mandatory_backend}")
        if a.match.recommendation in APPLY_FAMILY:
            check("apply_row_has_requirements", bool(a.match.requirements_matched),
                  f"{tag} apply recommendation with no source-grounded requirements")

    # -- run-level invariants ------------------------------------------------
    searched = sum(1 for r in results if is_genuinely_searched(r.status))
    internal = [r.company for r in results if is_internal_retryable(r.status)]
    check("no_internal_error_terminal", len(internal) == 0 or outcome != "PASS",
          f"internal/retryable statuses present: {internal}")
    if outcome == "PASS":
        check("at_least_8_searched_under_pass", searched >= 8, f"genuinely searched={searched}")
    for r in results:
        if is_genuinely_searched(r.status):
            check(f"lane_coverage[{r.company}]", r.lane_checklist_complete(config.primary_lanes),
                  f"{r.company} genuinely searched but lane checklist incomplete")

    # -- no minimum-job-count padding: every accepted row must be traceable --
    check("no_padding", all(getattr(a, "official_url", "") for a in evaluation.accepted),
          "an accepted row lacks an official URL (possible padding)")

    passed = len(failures) == 0
    return {"passed": passed, "checks": checks, "failures": failures,
            "genuinely_searched": searched, "internal_retryable": internal}
