"""Non-bypassable validation for the CLI-Playwright backend.

Two layers, both deterministic Python (the agent only ever *proposes*):

1. :func:`validate_result_object` — the result-envelope contract
   (schema, id match, single object, browser/MCP evidence, honest external block,
   no card-only PASS).
2. :func:`validate_job_evidence` — per proposed accepted job, re-derived from the
   captured source evidence using the existing Atlas validators
   (geography, title, experience, role family, stack anchors, quote grounding,
   closed-posting rejection).

:func:`build_validated_result` composes both into a
:class:`atlas.pilot.models.CompanySearchResult` with an independently-computed
accepted list — the agent cannot force a row in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from atlas.pilot.models import (
    CompanySearchResult, JobDetailEvidence, JobRejection, LaneCoverage, PRIMARY_LANES,
)
from atlas.pilot.normalize import normalize_source_text
from atlas.pilot.status_v4 import SEARCH_SUCCESS, EXTERNAL_BLOCKER, is_internal_retryable
from atlas.policy.rules import extract_experience

try:  # reuse the exact V4 role/stack/title logic where available
    from atlas.hunt.role_family import RoleFamily, classify_role_family
    from atlas.hunt.signals import signal_present, strip_alternative_language_enumerations
    from atlas.hunt.stack_evidence import analyze_technology_evidence
    from atlas.pilot.agentic_tools import looks_like_job_title
    _HUNT = True
except Exception:  # pragma: no cover - defensive; core modules should import
    _HUNT = False

    def signal_present(text: str, token: str) -> bool:
        return token.lower() in (text or "").lower()

    def strip_alternative_language_enumerations(text: str) -> str:
        return text or ""

    def looks_like_job_title(title: str) -> bool:
        return bool(title and title.strip()) and len(title.split()) <= 14

HARD_MIN_YEARS = 4.0

_JAVA_ANCHORS = ("java", "jvm", "spring", "spring boot", "spring mvc", "jakarta ee", "j2ee", "javaee")
_DOTNET_ANCHORS = (".net", ".net core", "c#", "asp.net", "asp.net core", "dotnet")
_FRONTEND_ANCHORS = ("react", "reactjs", "react.js", "angular", "vue", "vue.js", "javascript", "typescript")

_INDIA_TOKENS = (
    "india", "bharat", "bengaluru", "bangalore", "hyderabad", "pune", "chennai",
    "mumbai", "new delhi", "delhi", "gurgaon", "gurugram", "noida", "kolkata",
    "ahmedabad", "jaipur", "indore", "kochi", "cochin", "coimbatore", "chandigarh",
    "trivandrum", "thiruvananthapuram", "mysore", "mysuru", "nagpur",
    "visakhapatnam", "vizag", "bhubaneswar", "gandhinagar", "nashik", "vadodara",
)
_FOREIGN_TOKENS = (
    "united states", "u.s.", "usa", "united kingdom", "u.k.", " uk", "london",
    "germany", "france", "spain", "poland", "ireland", "netherlands", "canada",
    "australia", "singapore", "philippines", "malaysia", "japan", "china",
    "brazil", "mexico", "romania", "hungary", "czech", "portugal", "sweden",
    "switzerland", "dubai", "uae", "abu dhabi", "qatar", "saudi", "egypt",
    "south africa", "new york", "san francisco", "seattle", "toronto", "dublin",
    "manila", "kuala lumpur", "sao paulo", "buenos aires",
)
_CLOSED_MARKERS = (
    "no longer accepting", "no longer available", "position filled", "this job is closed",
    "requisition closed", "posting has expired", "job has expired", "applications are closed",
    "we are no longer accepting", "this position has been filled", "req closed",
)

_ATS_HOSTS = (
    "greenhouse.io", "boards.greenhouse.io", "lever.co", "jobs.lever.co",
    "ashbyhq.com", "jobs.ashbyhq.com", "myworkdayjobs.com", "myworkdaysite.com",
    "smartrecruiters.com", "icims.com", "successfactors.com", "taleo.net",
    "eightfold.ai", "phenompeople.com", "avature.net", "brassring.com",
)


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def is_official_or_ats_url(url: str, official_domain: str) -> bool:
    if not isinstance(url, str) or not url.lower().startswith("https://"):
        return False
    host = _host(url)
    if not host:
        return False
    dom = (official_domain or "").lower().lstrip(".")
    if dom and (host == dom or host.endswith("." + dom)):
        return True
    return any(host == a or host.endswith("." + a) for a in _ATS_HOSTS)


def classify_india_location(location: str) -> str:
    """Return ``INDIA`` / ``FOREIGN`` / ``UNKNOWN`` for a location string.

    An India signal wins even in a multi-location string (the role is India
    eligible); a purely foreign string is FOREIGN; anything else is UNKNOWN.
    Only ``INDIA`` may enter accepted jobs.
    """
    low = " " + (location or "").lower() + " "
    if any(tok in low for tok in _INDIA_TOKENS):
        return "INDIA"
    if any(tok in low for tok in _FOREIGN_TOKENS):
        return "FOREIGN"
    if "remote" in low and "india" in low:
        return "INDIA"
    return "UNKNOWN"


def _job_evidence_text(job: dict) -> str:
    # NOTE: evidence_snippets are deliberately excluded — quotes must be grounded
    # in the captured SOURCE (description/requirements), never in themselves.
    parts = [
        str(job.get("title", "")), str(job.get("description", "")),
        str(job.get("experience_text", "")), str(job.get("eligibility_text", "")),
    ]
    for k in ("mandatory_requirements", "preferred_requirements"):
        v = job.get(k) or []
        if isinstance(v, (list, tuple)):
            parts.extend(str(x) for x in v)
    return normalize_source_text(" \n ".join(p for p in parts if p))


def _has_any(text: str, tokens) -> bool:
    low = text.lower()
    return any(signal_present(low, t) for t in tokens)


@dataclass
class JobValidation:
    accepted: Optional[JobDetailEvidence] = None
    rejection: Optional[JobRejection] = None
    failures: tuple[str, ...] = ()


def validate_job_evidence(job: dict, *, candidate_max_years: Optional[float] = None,
                          official_domain: str = "", company: str = "") -> JobValidation:
    """Independently verify one proposed accepted job. Returns either an accepted
    :class:`JobDetailEvidence` or a :class:`JobRejection` (never both)."""
    title = str(job.get("title", "")).strip()
    url = str(job.get("official_url", "") or job.get("url", "")).strip()
    location = str(job.get("location", "")).strip()
    lane = str(job.get("lane", "")).strip().upper()
    ev_text = _job_evidence_text(job)
    tag = f"{company or job.get('company','')}:{title}"

    def reject(code: str, detail: str) -> JobValidation:
        return JobValidation(
            rejection=JobRejection(title=title or "(untitled)", lane=lane, reason_code=code,
                                   detail=detail, location=location, url=url),
            failures=(f"{code}: {detail}",),
        )

    # 1) HTTPS official employer/ATS URL
    if not is_official_or_ats_url(url, official_domain):
        return reject("NON_OFFICIAL_URL", f"{tag} url not https official/ATS: {url!r}")
    # 2) real, job-like title
    if not title or not looks_like_job_title(title):
        return reject("NOT_A_JOB_TITLE", f"{tag} title not job-like (banner/empty?)")
    # 3) India-only geography
    geo = classify_india_location(location)
    if geo != "INDIA":
        return reject("NON_INDIA_LOCATION", f"{tag} location={location!r} -> {geo}")
    # 4) full job-detail evidence captured (not a card)
    has_detail = bool(str(job.get("description", "")).strip()) or bool(job.get("mandatory_requirements"))
    if not has_detail:
        return reject("CARD_ONLY", f"{tag} no job-detail evidence captured (card only)")
    # 5) every evidence quote must exist in normalized source text
    snippets = [str(s) for s in (job.get("evidence_snippets") or []) if str(s).strip()]
    if not snippets:
        return reject("NO_EVIDENCE_QUOTES", f"{tag} no evidence snippets")
    norm_all = ev_text
    for s in snippets:
        if normalize_source_text(s).strip().lower() not in norm_all.lower():
            return reject("UNGROUNDED_QUOTE", f"{tag} quote not found in source: {s[:60]!r}")
    # 6) closed / inactive rejected
    if any(m in norm_all.lower() for m in _CLOSED_MARKERS):
        return reject("CLOSED_POSTING", f"{tag} closed/inactive evidence present")
    # 7) experience gate (post HTML/Unicode normalization; "Java 8" is a version)
    exp = extract_experience(norm_all)
    min_years = getattr(exp, "min_years", None)
    if min_years is not None and min_years >= HARD_MIN_YEARS:
        if candidate_max_years is None or candidate_max_years < min_years:
            return reject("EXPERIENCE_TOO_HIGH", f"{tag} mandatory min_years={min_years}")
    # 8) role family + stack anchors
    anchor_text = strip_alternative_language_enumerations(norm_all)
    if _HUNT:
        rf = classify_role_family(title, norm_all)
        if rf.family in (RoleFamily.ARCHITECTURE_ADVISORY.value, RoleFamily.TECH_LEAD.value):
            return reject("ROLE_FAMILY_NOT_PERMITTED", f"{tag} role_family={rf.family}")
        tech = analyze_technology_evidence(
            title, norm_all, tuple(job.get("mandatory_requirements") or ()),
            tuple(job.get("preferred_requirements") or ()),
        )
        if tech.unsupported_mandatory_backend and not tech.supported_backend:
            return reject("UNSUPPORTED_BACKEND",
                          f"{tag} unsupported mandatory backend {tech.unsupported_mandatory_backend}")
    if lane in ("JAVA_BACKEND", "JAVA_FULLSTACK") and not _has_any(anchor_text, _JAVA_ANCHORS):
        return reject("MISSING_JAVA_ANCHOR", f"{tag} lane {lane} lacks Java/JVM/Spring anchor")
    if lane == "JAVA_FULLSTACK" and not _has_any(anchor_text, _FRONTEND_ANCHORS):
        return reject("MISSING_FRONTEND_ANCHOR", f"{tag} java-fullstack lacks named frontend tech")
    if lane == "DOTNET" and not _has_any(anchor_text, _DOTNET_ANCHORS):
        return reject("MISSING_DOTNET_ANCHOR", f"{tag} lane DOTNET lacks C#/.NET/ASP.NET anchor")
    if lane == "REACT_FRONTEND" and not _has_any(anchor_text, ("react", "reactjs", "react.js", "frontend", "front end", "ui engineer")):
        return reject("NOT_FRONTEND_FOCUSED", f"{tag} react lane not frontend-focused")

    accepted = JobDetailEvidence(
        title=title, company=company or str(job.get("company", "")), location=location,
        work_mode=str(job.get("work_mode", "")), description=str(job.get("description", "")),
        mandatory_requirements=tuple(str(x) for x in (job.get("mandatory_requirements") or ())),
        preferred_requirements=tuple(str(x) for x in (job.get("preferred_requirements") or ())),
        experience_text=str(job.get("experience_text", "")),
        posted_date=str(job.get("posted_date", "")), updated_date=str(job.get("updated_date", "")),
        requisition_id=str(job.get("requisition_id", "")), official_url=url,
        eligibility_text=str(job.get("eligibility_text", "")),
        source_family=str(job.get("source_family", "")),
        evidence_snippets=tuple(snippets),
    )
    return JobValidation(accepted=accepted)


def validate_result_object(obj: Optional[dict], task, *, custom_site: bool = True) -> dict:
    """Validate the result envelope. Returns a dict with ``failures`` (list),
    ``browser_evidence`` (bool), ``external_block`` (bool)."""
    failures: list[str] = []
    if obj is None:
        return {"failures": ["MISSING_RESULT: no result object emitted"],
                "browser_evidence": False, "external_block": False}
    if not isinstance(obj, dict):
        return {"failures": ["MALFORMED_RESULT: not a JSON object"],
                "browser_evidence": False, "external_block": False}

    company = str(obj.get("company", ""))
    if company.strip().lower() != task.company.strip().lower():
        failures.append(f"COMPANY_MISMATCH: {company!r} != {task.company!r}")
    for key, want in (("task_id", task.task_id), ("run_id", task.run_id)):
        got = str(obj.get(key, ""))
        if got and got != str(want):
            failures.append(f"{key.upper()}_MISMATCH: {got!r} != {want!r}")

    status = str(obj.get("status", ""))
    evidence_urls = [u for u in (obj.get("evidence_urls") or []) if str(u).strip()]
    jobs = obj.get("jobs") or []
    browser_evidence = bool(obj.get("browser_evidence")) or bool(evidence_urls) or bool(
        obj.get("observed_result_state") in ("results_observed", "no_results", "external_block")
    )
    external_block = bool(obj.get("external_block")) or status in EXTERNAL_BLOCKER

    # zero browser/MCP evidence for a custom-site completion is invalid
    if custom_site and status in SEARCH_SUCCESS and not browser_evidence:
        failures.append("NO_BROWSER_EVIDENCE: searched custom site with zero browser/MCP evidence")

    # claimed external block without browser evidence
    if external_block and not (bool(obj.get("external_block_evidence")) or browser_evidence):
        failures.append("UNPROVEN_EXTERNAL_BLOCK: external block claimed without browser evidence")

    # card-only PASS: a WITH_MATCHES status whose jobs carry no detail evidence
    if status == "SEARCHED_COMPLETE_WITH_MATCHES":
        if not jobs:
            failures.append("CARD_ONLY_PASS: with-matches status but no jobs")
        else:
            detailed = any(
                (str(j.get("description", "")).strip() or j.get("mandatory_requirements"))
                and (str(j.get("evidence_snippets") and j.get("evidence_snippets")[0] or "").strip()
                     if j.get("evidence_snippets") else False)
                for j in jobs if isinstance(j, dict)
            )
            if not detailed:
                failures.append("CARD_ONLY_PASS: with-matches jobs carry no detail evidence")

    if status and status not in (SEARCH_SUCCESS | EXTERNAL_BLOCKER) and not is_internal_retryable(status):
        failures.append(f"UNKNOWN_STATUS: {status!r}")

    return {"failures": failures, "browser_evidence": browser_evidence, "external_block": external_block}


def build_validated_result(obj: dict, task, *, candidate_max_years: Optional[float] = None) -> CompanySearchResult:
    """Compose a validated :class:`CompanySearchResult` from a (contract-valid)
    proposed object, independently re-validating each accepted job."""
    accepted: list[JobDetailEvidence] = []
    rejections: list[JobRejection] = []
    for j in (obj.get("jobs") or []):
        if not isinstance(j, dict):
            continue
        v = validate_job_evidence(
            j, candidate_max_years=candidate_max_years,
            official_domain=str(obj.get("official_domain", task.official_domain)),
            company=task.company,
        )
        if v.accepted is not None:
            accepted.append(v.accepted)
        elif v.rejection is not None:
            rejections.append(v.rejection)
    for r in (obj.get("rejections") or []):
        if isinstance(r, dict) and r.get("title"):
            rejections.append(JobRejection(
                title=str(r.get("title")), lane=str(r.get("lane", "")),
                reason_code=str(r.get("reason_code", "AGENT_REJECTED")),
                detail=str(r.get("detail", "")), location=str(r.get("location", "")),
                url=str(r.get("url", "")),
            ))

    lanes: dict[str, LaneCoverage] = {}
    for lane in PRIMARY_LANES:
        cov = LaneCoverage(lane=lane)
        cov.attempted = lane in set(str(x).upper() for x in (obj.get("lanes_attempted") or task.lanes))
        lanes[lane] = cov

    status = str(obj.get("status", ""))
    if status == "SEARCHED_COMPLETE_WITH_MATCHES" and not accepted:
        status = "SEARCHED_COMPLETE_NO_MATCHES"

    return CompanySearchResult(
        company=task.company,
        official_domain=str(obj.get("official_domain", task.official_domain)),
        career_entry_url=str(obj.get("career_entry_url", task.career_entry_url)),
        route=str(obj.get("route", "cli_playwright")),
        source_family=str(obj.get("source_family", "custom")),
        status=status,
        lanes=lanes,
        jobs=accepted,
        rejections=rejections,
        limitations=[str(x) for x in (obj.get("limitations") or [])],
        evidence_urls=[str(x) for x in (obj.get("evidence_urls") or [])],
        model=str(obj.get("model", "")),
        task_id=task.task_id,
        queries_attempted=[str(x) for x in (obj.get("queries_attempted") or [])],
    )


__all__ = [
    "HARD_MIN_YEARS", "JobValidation", "is_official_or_ats_url", "classify_india_location",
    "validate_job_evidence", "validate_result_object", "build_validated_result",
]
