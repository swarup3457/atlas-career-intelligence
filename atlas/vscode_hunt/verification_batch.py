from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from atlas.browser_backend.validation import validate_job_evidence

from .models import LeadClassification, TASK_CONTRACT_VERSION, TaskKind


VERIFICATION_BATCH_SCHEMA_VERSION = 1
MAX_VERIFICATION_LEADS = 8


@dataclass(frozen=True)
class VerificationLead:
    lead_id: str
    company: str
    title_hint: str = ""
    location_hint: str = ""
    source_provider: str = ""
    source_url: str = ""
    official_url: str = ""
    official_domain: str = ""
    role_family_hint: str = ""
    posted_date_hint: str = ""
    provenance: Mapping[str, Any] = None  # type: ignore[assignment]
    allowed_domains: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "lead_id": self.lead_id, "company": self.company,
            "title_hint": self.title_hint, "location_hint": self.location_hint,
            "source_provider": self.source_provider, "source_url": self.source_url,
            "official_url": self.official_url, "official_domain": self.official_domain,
            "role_family_hint": self.role_family_hint, "posted_date_hint": self.posted_date_hint,
            "provenance": dict(self.provenance or {}), "allowed_domains": list(self.allowed_domains),
        }


def manifest_hash(manifest: Mapping[str, Any]) -> str:
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _assigned_ids(result: Mapping[str, Any]) -> list[str]:
    return [str(item) for item in result.get("assigned_lead_ids", [])]


def validate_verification_batch_result(result: object, task: Mapping[str, Any]) -> list[str]:
    if not isinstance(result, Mapping):
        return ["result must be a JSON object"]
    errors: list[str] = []
    required = {"schema_version", "run_id", "task_id", "attempt_id", "batch_id", "manifest_hash", "assigned_lead_ids", "outcomes", "completion_claim"}
    errors.extend(f"missing field: {key}" for key in sorted(required - set(result)))
    if result.get("schema_version") != VERIFICATION_BATCH_SCHEMA_VERSION:
        errors.append("unsupported verification batch schema_version")
    for key in ("run_id", "task_id", "attempt_id"):
        if str(result.get(key, "")) != str(task.get(key, "")):
            errors.append(f"{key} mismatch")
    if str(result.get("manifest_hash", "")) != str(task.get("manifest_hash", "")):
        errors.append("manifest_hash mismatch")
    assigned = _assigned_ids(result)
    if len(assigned) != len(set(assigned)):
        errors.append("duplicate assigned lead ID")
    task_ids = [str(item) for item in task.get("assigned_lead_ids", [])]
    if sorted(assigned) != sorted(task_ids):
        errors.append("assigned lead IDs mismatch")
    outcomes = result.get("outcomes")
    if not isinstance(outcomes, list):
        return errors + ["outcomes must be a list"]
    outcome_ids = [str(item.get("lead_id", "")) for item in outcomes if isinstance(item, Mapping)]
    if len(outcome_ids) != len(outcomes) or len(outcome_ids) != len(set(outcome_ids)):
        errors.append("outcomes must contain unique lead IDs")
    unknown = sorted(set(outcome_ids) - set(task_ids))
    if unknown:
        errors.append(f"unknown lead IDs: {unknown}")
    valid = {item.value for item in LeadClassification}
    for outcome in outcomes:
        if not isinstance(outcome, Mapping):
            errors.append("outcome must be an object")
            continue
        lead_id = str(outcome.get("lead_id", ""))
        if str(outcome.get("classification", "")) not in valid:
            errors.append(f"invalid classification for lead {lead_id}")
        classification = str(outcome.get("classification", ""))
        if classification in {LeadClassification.VERIFIED_ACCEPTED.value, LeadClassification.VERIFIED_STRETCH.value}:
            proposal = dict(outcome)
            proposal.setdefault("official_url", outcome.get("official_url", ""))
            proposal.setdefault("description", outcome.get("detail_text", ""))
            proposal.setdefault("evidence_snippets", outcome.get("evidence_quotes", []))
            validation = validate_job_evidence(
                proposal, official_domain=str(outcome.get("official_domain", "")),
                company=str(outcome.get("company", "")),
            )
            if validation.rejection:
                errors.append(f"lead {lead_id} rejected: {validation.rejection.reason_code}")
    return errors


def evaluate_verification_batch_completion(result: Mapping[str, Any], task: Mapping[str, Any]) -> dict[str, Any]:
    assigned = {str(item) for item in task.get("assigned_lead_ids", [])}
    outcomes = result.get("outcomes") if isinstance(result.get("outcomes"), list) else []
    observed = {str(item.get("lead_id", "")) for item in outcomes if isinstance(item, Mapping)}
    missing = sorted(assigned - observed)
    errors = validate_verification_batch_result(result, task)
    identity_errors = [error for error in errors if "mismatch" in error or "unknown lead" in error or "duplicate" in error]
    if identity_errors:
        action = "REJECTED_INVALID_RESULT"
    elif errors or missing:
        action = "FOLLOW_UP_REQUIRED"
    elif result.get("completion_claim") is False:
        action = "NEEDS_REPAIR"
    else:
        action = "COMPLETE"
    return {"action": action, "errors": errors, "missing_lead_ids": missing}


__all__ = [
    "MAX_VERIFICATION_LEADS", "VERIFICATION_BATCH_SCHEMA_VERSION", "VerificationLead",
    "evaluate_verification_batch_completion", "manifest_hash", "validate_verification_batch_result",
]