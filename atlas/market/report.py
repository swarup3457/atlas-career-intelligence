"""Market run report writer (Phase 1D §17).

ONE report writer that reads the run-scoped SQLite state AFTER fan-in and emits a
single current-run 8-sheet workbook via the proven atomic Excel writer. It
preserves SEPARATE axes and NEVER conflates them:

    * source type (official vs portal);
    * portal lead status (PORTAL_CURRENT_LEAD / LINKED_OFFICIAL_VERIFIED / …);
    * official verification level (VERIFIED_OFFICIAL / OFFICIAL_SEARCH_LIVE / …);
    * job lifecycle (ACTIVE / CLOSED / UNKNOWN);
    * freshness;
    * recommendation is a NOT_EVALUATED placeholder (candidate intelligence is
      out of scope for Phase 1D).

Only CURRENT-run records are included — a portal lead / official observation from
a prior run never inflates this run's summary.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from atlas.config import Settings
from atlas.persistence.sqlite import StateStore
from atlas.reporting.mapping import load_report_mapping, validate_report, write_report

_PORTAL_FAMILIES = {"linkedin", "naukri", "foundit", "indeed", "portal_generic"}


def _official_rows(store, run_id: str) -> list[dict[str, Any]]:
    decisions = {d["canonical_id"]: d for d in store.list_verification_decisions(run_id) if d["canonical_id"]}
    best: dict[str, dict] = {}
    rank = {"OFFICIAL_DETAIL_LIVE": 3, "OFFICIAL_SEARCH_LIVE": 2, "PORTAL_LIVE": 1}
    for row in store.list_raw_observations(run_id):
        fam = (row["source_family"] or "").lower()
        if fam in _PORTAL_FAMILIES:
            continue  # portals are separate rows below
        try:
            detail = json.loads(row["detail_json"] or "{}")
        except (ValueError, TypeError):
            detail = {}
        evidence = detail.get("verification_level") or ""
        key = row["canonical_id"] or row["source_identity"] or row["observation_id"]
        r = rank.get(evidence, 0)
        if key in best and best[key]["_rank"] >= r:
            continue
        decision = decisions.get(row["canonical_id"]) if row["canonical_id"] else None
        verification = decision["verification_level"] if decision else (
            "VERIFIED_OFFICIAL" if evidence == "OFFICIAL_DETAIL_LIVE" else
            "OFFICIAL_SEARCH_LIVE" if evidence.startswith("OFFICIAL") else "MANUAL_VERIFICATION")
        lifecycle = (decision["lifecycle_result"] if decision else None) or (
            "ACTIVE" if (row["is_active"] or "").upper() == "ACTIVE" else
            "CLOSED" if (row["is_active"] or "").upper() == "INACTIVE" else "UNKNOWN")
        best[key] = {
            "_rank": r,
            "company": row["company"], "role_title": row["title"], "source_job_id": row["source_job_id"],
            "location": row["location"], "work_mode": detail.get("work_mode") or "",
            "experience_text": detail.get("experience_text") or "", "lane": row["lane"] or "",
            "company_type": "OFFICIAL", "discovery_source": fam or "official",
            "source_url": row["source_url"] or "", "official_apply_url": row["canonical_url"] or "",
            "posted_date": row["posted_at"] or "",
            "freshness_band": (decision["freshness_result"] if decision else "") or "",
            "verification_level": verification, "job_lifecycle_status": lifecycle,
            "recommendation": "NOT_EVALUATED", "notes": "official evidence",
        }
    return list(best.values())


def _portal_rows(store, run_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for r in store.list_portal_leads(run_id):
        rows.append({
            "company": r["company_name"], "role_title": r["title"], "source_job_id": r["portal_job_id"],
            "location": r["location"], "work_mode": r["work_mode"] or "",
            "experience_text": r["experience_text"] or "", "lane": r["lane"] or "",
            "company_type": "PORTAL_LEAD", "discovery_source": r["source_family"],
            "source_url": r["canonical_url"] or "", "official_apply_url": r["official_apply_url"] or "",
            "posted_date": r["posted_text"] or "", "freshness_band": "PORTAL_LISTED",
            "verification_level": r["verification_state"], "job_lifecycle_status": (
                "CLOSED" if r["verification_state"] == "CLOSED_POSITIVE_EVIDENCE" else "PORTAL_ACTIVE"),
            "recommendation": "NOT_EVALUATED", "salary_text": r["salary_text"] or "",
            "notes": f"portal lead ({r['source_family']})",
        })
    return rows


def write_market_report(settings: Settings, run_id: str, campaign_id: str,
                        out_path: Path) -> tuple[Optional[str], bool]:
    mapping = load_report_mapping()
    with StateStore(settings.state_db) as store:
        official = _official_rows(store, run_id)
        portal = _portal_rows(store, run_id)
        all_jobs = sorted(official + portal, key=lambda r: (r.get("company") or "", r.get("role_title") or ""))

        # New (dynamically discovered) companies this run.
        new_companies = []
        seen_co = set()
        for prov in store.list_dynamic_company_provenance(run_id=run_id):
            cid = prov["company_id"]
            if not cid or cid in seen_co:
                continue
            seen_co.add(cid)
            co = store.get_company(cid)
            new_companies.append({
                "Company": (co["display_name"] if co else prov["normalized_name"]) or "",
                "Official_Domain": prov["resolved_domain"] or (co["official_domain"] if co else "") or "",
                "Company_Type": "DYNAMICALLY_DISCOVERED",
                "Source_Discovered_From": prov["discovery_source"] or "",
                "Reason_Added": prov["resolution_status"] or "",
            })

        # Source coverage per family (official + portal).
        fam_stats: dict[str, dict] = {}
        for r in official:
            f = fam_stats.setdefault(r["discovery_source"], {"raw": 0, "relevant": 0, "verified": 0, "portal": 0})
            f["raw"] += 1
            f["relevant"] += 1
            if r["verification_level"] == "VERIFIED_OFFICIAL":
                f["verified"] += 1
        for r in portal:
            f = fam_stats.setdefault(r["discovery_source"], {"raw": 0, "relevant": 0, "verified": 0, "portal": 0})
            f["raw"] += 1
            f["relevant"] += 1
            f["portal"] += 1
            if r["verification_level"] in ("LINKED_OFFICIAL_VERIFIED", "CLOSED_POSITIVE_EVIDENCE"):
                f["verified"] += 1
        source_cov = [{
            "Source": fam, "Attempted": "YES", "Completed": "COMPLETED",
            "Raw_Results": s["raw"], "Relevant_Results": s["relevant"],
            "Verified_Official": s["verified"], "Portal_Only": s["portal"], "Notes": "",
        } for fam, s in sorted(fam_stats.items())]

        # Closed leads.
        closed = [{
            "Company": r["company"], "Role": r["role_title"], "Job_ID": r["source_job_id"],
            "Location": r["location"], "Source": r["discovery_source"], "Reason": "closed positive evidence",
            "Verification_Status": r["verification_level"], "Live_Status": "CLOSED",
        } for r in portal if r["verification_level"] == "CLOSED_POSITIVE_EVIDENCE"]

        # Company coverage (from portal leads grouped by company).
        by_company: dict[str, dict] = {}
        for r in all_jobs:
            c = by_company.setdefault(r["company"] or "?", {"relevant": 0, "type": r["company_type"]})
            c["relevant"] += 1
        company_cov = [{
            "Company": name, "Company_Type": d["type"], "Result": "COMPLETED",
            "Relevant_Jobs": d["relevant"],
        } for name, d in sorted(by_company.items())]

        campaign = store.get_market_campaign(campaign_id)
        budget = store.get_campaign_budget(campaign_id)
        verified_official = sum(1 for r in all_jobs if r["verification_level"] in
                                ("VERIFIED_OFFICIAL", "LINKED_OFFICIAL_VERIFIED"))
        portal_only = sum(1 for r in portal if r["verification_level"] == "PORTAL_CURRENT_LEAD")
        run_summary = [{
            "Run_ID": run_id,
            "Run_Status": campaign["status"] if campaign else "UNKNOWN",
            "New_Companies_Discovered": len(new_companies),
            "LinkedIn_Checked": "YES" if "linkedin" in fam_stats else "NO",
            "Naukri_Checked": "YES" if "naukri" in fam_stats else "NO",
            "Raw_Discoveries": len(all_jobs),
            "Relevant_Discoveries": len(all_jobs),
            "Verified_Official": verified_official,
            "Portal_Only": portal_only,
            "Remaining_Work": (campaign["terminal_reason"] if campaign else "") or "",
        }]

    data = {
        "All_Jobs": all_jobs,
        "New_Companies": new_companies,
        "Company_Coverage": company_cov,
        "Source_Coverage": source_cov,
        "Closed_or_Rejected": closed,
        "Resume_Tailoring": [],
        "Recruiter_Contacts": [],
        "Run_Summary": run_summary,
    }
    result = write_report(mapping, Path(out_path), data)
    validation = validate_report(mapping, Path(result.written_path))
    return result.written_path, validation.ok


__all__ = ["write_market_report"]
