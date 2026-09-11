"""Cumulative job history + master workbook (PRODUCTION R1 §3, §15).

A prior verified job must never disappear just because a later run searched
different companies. This module records each run's independently-validated jobs
into the existing canonical-job store (``canonical_jobs`` / ``job_observations`` /
``status_history`` — reused, not duplicated) with the R1 status model, and builds
the cumulative master workbook.

Statuses (R1): NEW, ACTIVE, UPDATED, UNCHANGED, REVERIFY_REQUIRED, CLOSED,
SOURCE_UNAVAILABLE, REJECTED.

Rules enforced here:
- A prior job is never deleted; an inaccessible source moves it to
  REVERIFY_REQUIRED / SOURCE_UNAVAILABLE, never removes it.
- CLOSED requires explicit closure evidence.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Iterable

from openpyxl import Workbook, load_workbook

from atlas.persistence.sqlite import StateStore

# The payload marker that distinguishes an R1-verified canonical job from other
# canonical rows (e.g. legacy imports) so the master view stays scoped to R1.
R1_MARKER = "r1_verification"

CANONICAL_JOB_STATUSES = (
    "NEW", "ACTIVE", "UPDATED", "UNCHANGED",
    "REVERIFY_REQUIRED", "CLOSED", "SOURCE_UNAVAILABLE", "REJECTED",
)
ACTIVE_FAMILY = frozenset({"NEW", "ACTIVE", "UPDATED", "UNCHANGED"})
REVERIFY_FAMILY = frozenset({"REVERIFY_REQUIRED", "SOURCE_UNAVAILABLE"})
_APPLY_RECOMMENDATIONS = frozenset({"APPLY_NOW", "APPLY_AFTER_TAILORING", "STRETCH"})

MASTER_SHEETS = (
    "Active_Verified_Jobs", "Reverify_Required", "Closed_Jobs", "Rejected_History",
    "Company_History", "Source_Health_History", "Run_History", "Action_Queue",
)


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def canonical_job_id(official_url: str | None, company: str | None, requisition_id: str | None) -> str:
    """Stable canonical id with URL-first identity precedence."""
    url = (official_url or "").strip().lower().rstrip("/")
    if url:
        return "job::url:" + url
    key = "|".join(part.strip().lower() for part in (company or "", requisition_id or "") if part)
    seed = key or (company or "unknown")
    return "job::" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def _content_hash(evidence: Any, recommendation: str) -> str:
    payload = json.dumps({
        "title": evidence.title, "company": evidence.company, "location": evidence.location,
        "url": evidence.official_url, "req": evidence.requisition_id,
        "posted": evidence.posted_date, "rec": recommendation,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record_run_jobs(
    store: StateStore,
    *,
    run_id: str,
    validated_jobs: Iterable[Any],
    discovery_source: str = "",
) -> dict[str, Any]:
    """Record a run's validated jobs into the cumulative store. Idempotent per run."""
    summary = {"new": 0, "updated": 0, "unchanged": 0, "canonical_ids": []}
    now = _utcnow()
    for job in validated_jobs:
        evidence = job.evidence
        recommendation = getattr(job, "recommendation", "")
        cid = canonical_job_id(evidence.official_url, evidence.company, evidence.requisition_id)
        content_hash = _content_hash(evidence, recommendation)
        existing = store.get_canonical_job(cid)
        from_status = existing["current_status"] if existing else None
        payload = {
            R1_MARKER: getattr(job, "verification_status", "PYTHON_VALIDATED"),
            "last_verified_at": now, "recommendation": recommendation,
            "lane": getattr(job, "lane", ""), "run_id": run_id, "discovery_source": discovery_source,
        }

        if existing is None:
            to_status = "NEW"
            store.upsert_canonical_job(
                cid, company=evidence.company, job_id=evidence.requisition_id, role=evidence.title,
                location=evidence.location, status=to_status, first_seen=now, last_seen=now,
                source_url=evidence.official_url, official_apply_url=evidence.official_url,
                content_hash=content_hash, payload=payload,
            )
            summary["new"] += 1
        elif existing["content_hash"] != content_hash:
            to_status = "UPDATED"
            store.upsert_canonical_job(
                cid, company=evidence.company, job_id=evidence.requisition_id, role=evidence.title,
                location=evidence.location, status=to_status, first_seen=existing["first_seen"] or now,
                last_seen=now, source_url=evidence.official_url, official_apply_url=evidence.official_url,
                content_hash=content_hash, payload=payload,
            )
            summary["updated"] += 1
        else:
            to_status = "UNCHANGED"
            store.set_canonical_status(cid, to_status)
            summary["unchanged"] += 1

        store.add_observation(
            observation_id=f"{run_id}:{cid}", canonical_id=cid, record_id=run_id,
            discovery_source=discovery_source or "vscode_hunt", observed_status=to_status,
            observed_at=now, content_hash=content_hash,
            provenance={"run_id": run_id, "official_url": evidence.official_url, "recommendation": recommendation},
        )
        store.record_status_change(
            history_id=f"{run_id}:{cid}:{to_status}", canonical_id=cid, to_status=to_status,
            from_status=from_status, reason="verified_in_run", context={"run_id": run_id},
        )
        summary["canonical_ids"].append(cid)
    return summary


def _transition(store: StateStore, canonical_id: str, to_status: str, *, reason: str, run_id: str = "") -> None:
    existing = store.get_canonical_job(canonical_id)
    if existing is None:
        raise ValueError(f"unknown canonical job: {canonical_id}")
    from_status = existing["current_status"]
    store.set_canonical_status(canonical_id, to_status)
    store.record_status_change(
        history_id=f"{run_id or _utcnow()}:{canonical_id}:{to_status}:{uuid.uuid4().hex[:8]}",
        canonical_id=canonical_id, to_status=to_status, from_status=from_status,
        reason=reason, context={"run_id": run_id},
    )


def mark_reverify_required(store: StateStore, canonical_id: str, *, reason: str, run_id: str = "") -> None:
    """A prior job whose source could not be re-checked stays visible for recheck."""
    _transition(store, canonical_id, "REVERIFY_REQUIRED", reason=reason or "recheck_due", run_id=run_id)


def mark_source_unavailable(store: StateStore, canonical_id: str, *, reason: str, run_id: str = "") -> None:
    """An inaccessible source never silently deletes a prior job."""
    _transition(store, canonical_id, "SOURCE_UNAVAILABLE", reason=reason or "source_unavailable", run_id=run_id)


def mark_closed(store: StateStore, canonical_id: str, *, evidence: str, run_id: str = "") -> None:
    """CLOSED requires explicit closure / dead-detail evidence."""
    if not str(evidence).strip():
        raise ValueError("CLOSED requires explicit closure evidence")
    _transition(store, canonical_id, "CLOSED", reason=str(evidence), run_id=run_id)


def _is_r1(row: Any) -> bool:
    try:
        return R1_MARKER in json.loads(row["payload_json"] or "{}")
    except (ValueError, TypeError):
        return False


def _last_verified(store: StateStore, row: Any, payload: dict[str, Any]) -> str:
    observed = [o["observed_at"] for o in store.list_observations(row["canonical_id"]) if o["observed_at"]]
    if observed:
        return max(observed)
    return str(payload.get("last_verified_at") or row["last_seen"] or "")


def build_master_workbook(store: StateStore, output_root: Path, *, generated_at: datetime.datetime | None = None) -> Path:
    """Build the immutable cumulative master workbook (§15). Never overwrites."""
    stamp = (generated_at or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(output_root) / "production" / "master"
    out_dir.mkdir(parents=True, exist_ok=True)
    output = out_dir / f"Atlas_Master_Active_{stamp}.xlsx"
    if output.exists():
        output = out_dir / f"Atlas_Master_Active_{stamp}_{uuid.uuid4().hex[:6]}.xlsx"

    wb = Workbook()
    wb.remove(wb.active)
    for name in MASTER_SHEETS:
        wb.create_sheet(name)
    wb["Active_Verified_Jobs"].append(["canonical_id", "company", "title", "location", "official_url", "requisition", "status", "first_seen", "last_seen", "last_verified", "recommendation"])
    wb["Reverify_Required"].append(["canonical_id", "company", "title", "official_url", "status", "first_seen", "last_verified", "reason"])
    wb["Closed_Jobs"].append(["canonical_id", "company", "title", "official_url", "first_seen", "closed_at", "closure_evidence"])
    wb["Rejected_History"].append(["canonical_id", "company", "title", "official_url", "first_seen", "rejected_at", "reason"])
    wb["Company_History"].append(["company", "active", "reverify", "closed", "rejected", "first_seen", "last_seen"])
    wb["Source_Health_History"].append(["source_instance", "source_type", "state", "result_count", "reason", "observed_at"])
    wb["Run_History"].append(["run_id", "status", "company_count"])
    wb["Action_Queue"].append(["canonical_id", "company", "title", "official_url", "status", "recommended_action"])

    company_stats: dict[str, dict[str, Any]] = {}
    for row in store.list_canonical_jobs():
        if not _is_r1(row):
            continue
        payload = json.loads(row["payload_json"] or "{}")
        status = row["current_status"]
        company = row["company"] or ""
        url = row["official_apply_url"] or row["source_url"] or ""
        last_verified = _last_verified(store, row, payload)
        history = store.list_status_history(row["canonical_id"])
        latest_reason = history[-1]["reason"] if history else ""
        changed_at = history[-1]["changed_at"] if history else row["updated_at"]

        stats = company_stats.setdefault(company, {"active": 0, "reverify": 0, "closed": 0, "rejected": 0, "first_seen": row["first_seen"], "last_seen": row["last_seen"]})
        stats["first_seen"] = min(x for x in (stats["first_seen"], row["first_seen"]) if x) if stats["first_seen"] and row["first_seen"] else (stats["first_seen"] or row["first_seen"])
        stats["last_seen"] = max(x for x in (stats["last_seen"], row["last_seen"]) if x) if stats["last_seen"] and row["last_seen"] else (stats["last_seen"] or row["last_seen"])

        if status in ACTIVE_FAMILY:
            stats["active"] += 1
            wb["Active_Verified_Jobs"].append([row["canonical_id"], company, row["role"], row["location"], url, row["job_id"], status, row["first_seen"], row["last_seen"], last_verified, payload.get("recommendation", "")])
            if payload.get("recommendation") in _APPLY_RECOMMENDATIONS:
                wb["Action_Queue"].append([row["canonical_id"], company, row["role"], url, status, "APPLY"])
        elif status in REVERIFY_FAMILY:
            stats["reverify"] += 1
            wb["Reverify_Required"].append([row["canonical_id"], company, row["role"], url, status, row["first_seen"], last_verified, latest_reason])
            wb["Action_Queue"].append([row["canonical_id"], company, row["role"], url, status, "REVERIFY"])
        elif status == "CLOSED":
            stats["closed"] += 1
            wb["Closed_Jobs"].append([row["canonical_id"], company, row["role"], url, row["first_seen"], changed_at, latest_reason])
        elif status == "REJECTED":
            stats["rejected"] += 1
            wb["Rejected_History"].append([row["canonical_id"], company, row["role"], url, row["first_seen"], changed_at, latest_reason])

    for company, stats in sorted(company_stats.items()):
        wb["Company_History"].append([company, stats["active"], stats["reverify"], stats["closed"], stats["rejected"], stats["first_seen"], stats["last_seen"]])

    conn = store._conn
    for health in conn.execute("SELECT source_instance, source_type, state, result_count, reason, observed_at FROM source_health_history ORDER BY observed_at").fetchall():
        wb["Source_Health_History"].append([health["source_instance"], health["source_type"], health["state"], health["result_count"], health["reason"], health["observed_at"]])
    for run in conn.execute("SELECT run_id, status, company_count FROM vscode_hunt_runs ORDER BY run_id").fetchall():
        wb["Run_History"].append([run["run_id"], run["status"], run["company_count"]])

    wb.save(output)
    load_workbook(output).close()
    return output
