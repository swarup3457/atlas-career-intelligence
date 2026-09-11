from __future__ import annotations

import hashlib
import json
from urllib.parse import urlparse
from dataclasses import dataclass
from typing import Any, Mapping

from atlas.browser_backend.validation import validate_job_evidence

from .models import LeadClassification, TASK_CONTRACT_VERSION, TaskKind


VERIFICATION_BATCH_SCHEMA_VERSION = 1
MAX_VERIFICATION_LEADS = 8
ALLOWED_TERMINAL_CLASSIFICATIONS = tuple(item.value for item in LeadClassification)


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
    provenance: Mapping[str, Any] | None = None
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


def _host(value: str) -> str:
    return (urlparse(str(value)).hostname or "").lower()


def normalize_selection_manifest(
    manifest: Mapping[str, Any],
    *,
    parent_run_id: str | None = None,
    source_reference: str | None = None,
) -> dict[str, Any]:
    """Adapt the R1 selection shape into the sealed batch contract.

    Existing ``selected`` records remain source data: missing fields are left
    empty rather than inferred. Already-batched manifests are accepted too.
    """
    source = list(manifest.get("selected") or [])
    raw_batches = list(manifest.get("batches") or [])
    if raw_batches:
        batches = raw_batches
    elif source:
        batches = [
            {"batch_id": f"batch-{index // MAX_VERIFICATION_LEADS + 1:02d}", "leads": source[index:index + MAX_VERIFICATION_LEADS]}
            for index in range(0, len(source), MAX_VERIFICATION_LEADS)
        ]
    else:
        batches = []
    normalized_batches: list[dict[str, Any]] = []
    invalid: list[dict[str, str]] = []
    seen: set[str] = set()
    for batch_index, batch in enumerate(batches, 1):
        leads: list[dict[str, Any]] = []
        for lead_index, raw in enumerate(batch.get("leads") or [], 1):
            if not isinstance(raw, Mapping):
                invalid.append({"batch": str(batch_index), "reason": "lead is not an object"})
                continue
            url = str(raw.get("canonical_url") or raw.get("official_url") or raw.get("source_url") or raw.get("url") or "")
            lead_id = str(raw.get("lead_id") or raw.get("identity") or url or f"lead-{batch_index}-{lead_index}")
            if lead_id in seen:
                invalid.append({"lead_id": lead_id, "reason": "duplicate lead identity"})
                continue
            if not url:
                invalid.append({"lead_id": lead_id, "reason": "missing source URL"})
                continue
            seen.add(lead_id)
            official_domain = str(raw.get("official_domain") or raw.get("company_domain") or "")
            allowed = sorted({host for host in (_host(url), _host(official_domain)) if host})
            leads.append(VerificationLead(
                lead_id=lead_id,
                company=str(raw.get("company") or raw.get("company_name") or ""),
                title_hint=str(raw.get("title") or raw.get("title_hint") or ""),
                location_hint=str(raw.get("location") or raw.get("location_hint") or ""),
                source_provider=str(raw.get("source_provider") or raw.get("kind") or ""),
                source_url=str(raw.get("source_url") or raw.get("url") or url),
                official_url=str(raw.get("canonical_url") or raw.get("official_url") or ""),
                official_domain=official_domain,
                role_family_hint=str(raw.get("lane") or raw.get("role_family_hint") or ""),
                posted_date_hint=str(raw.get("posted_date") or raw.get("posted_date_hint") or ""),
                provenance={"identity": raw.get("identity", ""), "query": raw.get("query", ""), "skills": raw.get("skills", [])},
                allowed_domains=tuple(allowed),
            ).to_dict())
        normalized_batches.append({"batch_id": str(batch.get("batch_id") or f"batch-{batch_index:02d}"), "leads": leads})
    normalized = {
        "parent_run_id": parent_run_id or str(manifest.get("run_id") or ""),
        "source_reference": source_reference or str(manifest.get("source_reference") or ""),
        "selection_rules": str(manifest.get("selection_rules") or ""),
        "batches": normalized_batches,
        "invalid_leads": invalid,
    }
    normalized["normalized_manifest_hash"] = manifest_hash(normalized)
    return normalized


def _assigned_ids(result: Mapping[str, Any]) -> list[str]:
    return [str(item) for item in result.get("assigned_lead_ids", [])]


def validate_lead_outcome(outcome: Mapping[str, Any], sealed_lead: Mapping[str, Any]) -> list[str]:
    lead_id = str(outcome.get("lead_id", ""))
    errors: list[str] = []
    if str(outcome.get("company", "")).strip().lower() != str(sealed_lead.get("company", "")).strip().lower():
        errors.append(f"lead {lead_id}: company identity mismatch")
    classification = str(outcome.get("classification", ""))
    required = {
        "CLOSED": ("closure_evidence",),
        "FOREIGN": ("foreign_location_evidence",),
        "SOURCE_UNAVAILABLE": ("attempted_url", "error_details", "source_health"),
        "VERIFIED_REJECTED": ("reason_code", "evidence_quotes"),
        "DUPLICATE": ("duplicate_of",),
        "INTERNAL_ERROR": ("stage", "error_details"),
    }.get(classification, ())
    errors.extend(f"lead {lead_id}: missing {field}" for field in required if not outcome.get(field))
    if classification in {LeadClassification.VERIFIED_ACCEPTED.value, LeadClassification.VERIFIED_STRETCH.value}:
        url = str(outcome.get("official_url", ""))
        allowed = set(str(item).lower() for item in sealed_lead.get("allowed_domains", []))
        if _host(url) not in allowed:
            errors.append(f"lead {lead_id}: official URL is outside sealed allowed domains")
        proposal = dict(outcome)
        proposal.setdefault("description", outcome.get("detail_text", ""))
        proposal.setdefault("evidence_snippets", outcome.get("evidence_quotes", []))
        validation = validate_job_evidence(proposal, official_domain=str(sealed_lead.get("official_domain", "")), company=str(sealed_lead.get("company", "")))
        if validation.rejection:
            errors.append(f"lead {lead_id} rejected: {validation.rejection.reason_code}")
    return errors


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
    sealed_by_id = {str(item.get("lead_id")): item for item in task.get("leads", []) if isinstance(item, Mapping)}
    for outcome in outcomes:
        if not isinstance(outcome, Mapping):
            errors.append("outcome must be an object")
            continue
        lead_id = str(outcome.get("lead_id", ""))
        if str(outcome.get("classification", "")) not in valid:
            errors.append(f"invalid classification for lead {lead_id}")
        classification = str(outcome.get("classification", ""))
        if lead_id in sealed_by_id:
            errors.extend(validate_lead_outcome(outcome, sealed_by_id[lead_id]))
    return errors


def evaluate_verification_batch_completion(result: Mapping[str, Any], task: Mapping[str, Any]) -> dict[str, Any]:
    assigned = {str(item) for item in task.get("assigned_lead_ids", [])}
    outcomes = result.get("outcomes") if isinstance(result.get("outcomes"), list) else []
    observed = {str(item.get("lead_id", "")) for item in outcomes if isinstance(item, Mapping)}
    missing = sorted(assigned - observed)
    errors = validate_verification_batch_result(result, task)
    structural_errors = [
        error for error in errors
        if any(token in error for token in ("missing field:", "unsupported", "mismatch", "unknown lead", "duplicate", "assigned lead IDs"))
    ]
    if structural_errors:
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
    "ALLOWED_TERMINAL_CLASSIFICATIONS", "evaluate_verification_batch_completion", "manifest_hash",
    "normalize_selection_manifest", "validate_verification_batch_result",
    "validate_lead_outcome",
]