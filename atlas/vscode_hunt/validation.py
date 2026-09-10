from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlparse

from atlas.browser_backend.validation import validate_job_evidence

REQUIRED_RESULT_KEYS = {
    "schema_version", "run_id", "task_id", "attempt_id", "company_id", "worker_invocation_id",
    "official_domain", "career_url", "queries", "lanes_attempted", "result_states",
    "detail_urls", "jobs", "rejections", "foreign_leads", "browser_errors",
    "evidence_quotes", "source_health", "external_block_evidence", "completion_claim",
}


def _https(value: object) -> bool:
    parsed = urlparse(str(value))
    if not parsed.netloc:
        return False
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}


def validate_result(result: object, task: Mapping[str, object]) -> list[str]:
    if not isinstance(result, Mapping):
        return ["result must be a JSON object"]
    missing = sorted(REQUIRED_RESULT_KEYS - set(result))
    errors = [f"missing field: {key}" for key in missing]
    if result.get("schema_version") != 2: errors.append("unsupported schema_version")
    if str(result.get("run_id")) != str(task["run_id"]): errors.append("run_id mismatch")
    if str(result.get("task_id")) != str(task["task_id"]): errors.append("task_id mismatch")
    if not str(result.get("attempt_id", "")).strip(): errors.append("attempt_id is empty")
    if not _https(result.get("career_url")): errors.append("career_url must be HTTPS")
    if not isinstance(result.get("jobs"), list): errors.append("jobs must be a list")
    if not isinstance(result.get("evidence_quotes"), list): errors.append("evidence_quotes must be a list")
    if not isinstance(result.get("result_states"), (list, dict)): errors.append("result_states must be a list or object")
    if not isinstance(result.get("source_health"), dict): errors.append("source_health must be an object")
    if result.get("completion_claim") and not result.get("result_states"):
        errors.append("completion claim has no observed result states")
    if result.get("completion_claim") and result.get("browser_errors") and not result.get("external_block_evidence"):
        health = result.get("source_health") or {}
        usable = bool(health.get("results_pages_usable") or health.get("canonical_detail_pages_loaded") or health.get("official_google_careers_page_loaded") or health.get("search_form_usable") or health.get("detail_pages_usable") or str(health.get("status", "")).lower() in {"healthy", "ok", "usable"})
        if not usable:
            errors.append("internal browser errors cannot be claimed as complete without usable source-health evidence")
    official_domain = str(result.get("official_domain", ""))
    for index, job in enumerate(result.get("jobs") or []):
        if not isinstance(job, dict):
            errors.append(f"jobs[{index}] must be an object")
            continue
        if str(job.get("proposed_decision", "accept")).lower() not in {"accept", "accepted", "validate"}:
            continue
        proposal = dict(job)
        proposal.setdefault("official_url", proposal.get("canonical_url", ""))
        proposal.setdefault("description", proposal.get("detail_text", ""))
        proposal.setdefault("evidence_snippets", proposal.get("evidence_quotes", []))
        proposal.setdefault("company", task["company_name"])
        validation = validate_job_evidence(proposal, official_domain=official_domain, company=str(task["company_name"]))
        if validation.rejection:
            errors.append(f"jobs[{index}] rejected: {validation.rejection.reason_code}")
    return errors


def missing_obligations(result: Mapping[str, object], task: Mapping[str, object]) -> list[str]:
    missing: list[str] = []
    attempted = set(result.get("lanes_attempted") or [])
    try:
        lanes = task["lanes_json"]
    except (KeyError, IndexError):
        lanes = task.get("lanes", ())
    if isinstance(lanes, str):
        lanes = __import__("json").loads(lanes)
    for lane in lanes:
        if lane not in attempted:
            missing.append(f"lane:{lane}")
    if not result.get("career_url"):
        missing.append("official-career-url")
    if result.get("jobs") and not result.get("evidence_quotes"):
        missing.append("evidence-quotes-for-proposed-jobs")
    return missing
