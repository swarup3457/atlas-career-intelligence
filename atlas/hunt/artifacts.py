"""Load immutable run artifacts back into typed records.

Enables policy-only requalify and report-only rebuild WITHOUT re-fetching
(build spec 12.5, 20). Dates are parsed back from ISO strings.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Optional

from atlas.hunt.models import BoardSnapshot, JobDetailRevision, RawJob

__all__ = ["load_snapshots", "load_details", "snapshots_by_company", "details_by_company", "load_campaign"]


def _date(value: Optional[str]) -> Optional[datetime.date]:
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _raw_job(d: dict) -> RawJob:
    return RawJob(
        source_job_id=d.get("source_job_id", ""), title=d.get("title", ""),
        location=d.get("location", ""), url=d.get("url", ""), summary=d.get("summary", ""),
        posted_date=_date(d.get("posted_date")), requisition_id=d.get("requisition_id", ""),
    )


def load_snapshots(run_dir: Path) -> list[BoardSnapshot]:
    path = Path(run_dir) / "raw_observations.jsonl"
    out: list[BoardSnapshot] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        out.append(
            BoardSnapshot(
                snapshot_id=d["snapshot_id"], campaign_id=d["campaign_id"], company_id=d["company_id"],
                company_name=d["company_name"], source_instance_id=d["source_instance_id"],
                source_family=d["source_family"], route_family=d["route_family"], source_url=d.get("source_url", ""),
                pages=int(d.get("pages", 1)), adapter_version=d.get("adapter_version", ""),
                parser_version=d.get("parser_version", ""), source_health=d.get("source_health", "OK"),
                raw_jobs=tuple(_raw_job(j) for j in d.get("raw_jobs", [])),
                snapshot_status=d.get("snapshot_status", "COMPLETE"), access_status=d.get("access_status", "OK"),
            )
        )
    return out


def load_details(run_dir: Path) -> list[JobDetailRevision]:
    path = Path(run_dir) / "job_details.jsonl"
    out: list[JobDetailRevision] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        out.append(
            JobDetailRevision(
                revision_id=d["revision_id"], snapshot_id=d["snapshot_id"], source_job_id=d["source_job_id"],
                company=d["company"], title=d["title"], description=d.get("description", ""),
                mandatory_requirements=tuple(d.get("mandatory_requirements", [])),
                preferred_requirements=tuple(d.get("preferred_requirements", [])),
                experience_text=d.get("experience_text", ""), location=d.get("location", ""),
                work_mode=d.get("work_mode", "UNKNOWN"), posted_date=_date(d.get("posted_date")),
                deadline=_date(d.get("deadline")), requisition_id=d.get("requisition_id", ""),
                official_url=d.get("official_url", ""), verification_state=d.get("verification_state", "VERIFIED_OFFICIAL"),
                has_live_official_page=bool(d.get("has_live_official_page", True)),
                evidence_texts=tuple(d.get("evidence_texts", [])), eligibility_text=d.get("eligibility_text", ""),
                source_family=d.get("source_family", ""), parser_version=d.get("parser_version", ""),
                retrieved_at=d.get("retrieved_at", ""), record_class=d.get("record_class", "OFFICIAL_DIRECT"),
                discovery_channels=tuple(d.get("discovery_channels", [])),
            )
        )
    return out


def snapshots_by_company(snapshots) -> dict[str, BoardSnapshot]:
    return {s.company_name: s for s in snapshots}


def details_by_company(snapshots, details) -> dict[str, list[JobDetailRevision]]:
    by_snapshot: dict[str, str] = {s.snapshot_id: s.company_name for s in snapshots}
    out: dict[str, list[JobDetailRevision]] = {}
    for d in details:
        company = by_snapshot.get(d.snapshot_id, d.company)
        out.setdefault(company, []).append(d)
    return out


def load_campaign(run_dir: Path):
    """Reconstruct a lightweight CompanyCampaign from sealed_company_plan.json
    so a policy-only requalify/rebuild can recompute coverage over the same
    sealed obligations."""
    from atlas.hunt.campaign import CompanyCampaign, CompanyRef, SealedBatch

    plan_path = Path(run_dir) / "sealed_company_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    companies = tuple(
        CompanyRef(name=c["name"], group=c.get("group", ""), tier=c.get("tier", "A"), origin=c.get("origin", "SEED"))
        for c in plan.get("companies", [])
    )
    batch = SealedBatch(
        batch_id=f"{plan.get('campaign_id','')}-B0", batch_index=0,
        campaign_id=plan.get("campaign_id", ""), companies=companies,
        batch_hash="reloaded", sealed_at="", reason="reloaded",
    )
    return CompanyCampaign(
        campaign_id=plan.get("campaign_id", ""), created_at="",
        role_policy_hash=plan.get("role_policy_hash", ""),
        company_plan_hash=plan.get("company_plan_hash", ""),
        lanes=tuple(plan.get("lanes", [])), batches=[batch],
    )

