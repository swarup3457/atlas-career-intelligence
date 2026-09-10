"""Five-sheet recovery workbook (openpyxl) for an offline recovery run.

Report-only: generated from the recovery artifacts. Sheets: Validated_Jobs,
Rejected_Jobs, Company_Coverage, Evidence_Audit, Usage. Never updates the
production ``latest`` workbook.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from atlas.reporting.excel import ExcelReporter

_SHEETS = ("Validated_Jobs", "Rejected_Jobs", "Company_Coverage", "Evidence_Audit", "Usage")


def _jobs_rows(validated: Optional[dict], manifest: dict) -> list[dict]:
    rows: list[dict] = []
    parent = manifest.get("parent_run_id", "")
    for j in ((validated or {}).get("jobs") or []):
        rows.append({
            "parent_run_id": parent,
            "title": j.get("title", ""),
            "location": j.get("location", ""),
            "requisition_id": j.get("requisition_id", ""),
            "experience_text": j.get("experience_text", ""),
            "official_url": j.get("official_url", ""),
            "stack_evidence": " | ".join(
                [str(x) for x in (j.get("preferred_requirements") or [])]
                + [str(x) for x in (j.get("evidence_snippets") or [])]),
            "deterministic_decision": "ACCEPTED",
        })
    return rows


def _rejected_rows(validated: Optional[dict], contract: dict) -> list[dict]:
    rows: list[dict] = []
    for r in ((validated or {}).get("rejections") or []):
        rows.append({
            "title": r.get("title", ""),
            "lane": r.get("lane", ""),
            "reason_code": r.get("reason_code", ""),
            "detail": r.get("detail", ""),
            "location": r.get("location", ""),
            "url": r.get("url", ""),
        })
    for f in (contract.get("failures") or []):
        rows.append({"title": "(contract)", "lane": "", "reason_code": "CONTRACT_FAILURE",
                     "detail": f, "location": "", "url": ""})
    return rows


def _coverage_rows(manifest: dict, validated: Optional[dict]) -> list[dict]:
    return [{
        "company": (validated or {}).get("company", ""),
        "parent_run_id": manifest.get("parent_run_id", ""),
        "recovery_run_id": manifest.get("recovery_run_id", ""),
        "parser_false_negative": "PARSER_ERROR->recovered",
        "status": manifest.get("status", ""),
        "valid": manifest.get("valid", False),
        "completion_state": manifest.get("completion_state", ""),
        "official_url": (validated or {}).get("career_entry_url", ""),
        "no_browser": manifest.get("no_browser", True),
        "no_network": manifest.get("no_network", True),
        "no_model": manifest.get("no_model", True),
        "git_commit": manifest.get("git_commit", ""),
    }]


def _evidence_rows(manifest: dict, proposed: dict, source_evidence: list) -> list[dict]:
    rows: list[dict] = []
    se_by_req = {se.get("requisition_id", ""): se for se in (source_evidence or [])}
    for j in (proposed.get("jobs") or []):
        se = se_by_req.get(str(j.get("requisition_id", "")), {})
        rows.append({
            "requisition_id": j.get("requisition_id", ""),
            "title": j.get("title", ""),
            "official_url": j.get("official_url", ""),
            "selected_snapshot": manifest.get("selected_snapshot", ""),
            "matched_requisition": se.get("matched_requisition", ""),
            "matched_url": se.get("matched_url", ""),
            "experience_source": se.get("experience_text", ""),
            "grounded_quotes": " | ".join(str(x) for x in (j.get("evidence_snippets") or [])),
            "source_skills": " | ".join(str(x) for x in (se.get("skills") or [])),
        })
    if not rows:
        rows.append({"requisition_id": "", "title": "", "official_url": "",
                     "selected_snapshot": manifest.get("selected_snapshot", ""),
                     "matched_requisition": "", "matched_url": "",
                     "experience_source": "", "grounded_quotes": "", "source_skills": ""})
    return rows


def _usage_rows(manifest: dict, original_recorded_credits: float, corrected_credits: float) -> list[dict]:
    usage = manifest.get("usage", {})
    return [{
        "parent_run_id": manifest.get("parent_run_id", ""),
        "original_recorded_credits": original_recorded_credits,
        "corrected_credits": corrected_credits,
        "double_count_factor": usage.get("double_count_factor", ""),
        "precedence": "top_level_totalNanoAiu/1e9 -> top_level_credit -> single_child_aggregate",
        "browser_calls_during_recovery": 0,
        "network_calls_during_recovery": 0,
        "model_calls_during_recovery": 0,
    }]


def write_recovery_workbook(path: Path, *, manifest: dict, proposed: dict,
                            validated: Optional[dict], contract: dict,
                            source_evidence: list, original_recorded_credits: float,
                            corrected_credits: float) -> Path:
    reporter = ExcelReporter()
    sheets = [
        ("Validated_Jobs", None, _jobs_rows(validated, manifest)),
        ("Rejected_Jobs", None, _rejected_rows(validated, contract)),
        ("Company_Coverage", None, _coverage_rows(manifest, validated)),
        ("Evidence_Audit", None, _evidence_rows(manifest, proposed, source_evidence)),
        ("Usage", None, _usage_rows(manifest, original_recorded_credits, corrected_credits)),
    ]
    # Guarantee every sheet exists even when a section is empty.
    sheets = [(name, cols, rows or [{"note": "no rows"}]) for (name, cols, rows) in sheets]
    wb = reporter.build_workbook(sheets)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(path))
    return path


__all__ = ["write_recovery_workbook", "_SHEETS"]
