from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlparse

REQUIRED_RESULT_KEYS = {
    "run_id", "task_id", "attempt_id", "company_id", "worker_invocation_id",
    "official_domain", "career_url", "queries", "lanes_attempted", "result_states",
    "detail_urls", "jobs", "rejections", "foreign_leads", "browser_errors",
    "evidence_quotes", "completion_claim",
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
    if str(result.get("run_id")) != str(task["run_id"]): errors.append("run_id mismatch")
    if str(result.get("task_id")) != str(task["task_id"]): errors.append("task_id mismatch")
    if not str(result.get("attempt_id", "")).strip(): errors.append("attempt_id is empty")
    if not _https(result.get("career_url")): errors.append("career_url must be HTTPS")
    if not isinstance(result.get("jobs"), list): errors.append("jobs must be a list")
    if not isinstance(result.get("evidence_quotes"), list): errors.append("evidence_quotes must be a list")
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
