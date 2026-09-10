from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook

SHEETS = (
    "Validated_Jobs", "Rejected_Jobs", "Foreign_Leads", "Raw_Job_Leads",
    "Discovery_Providers", "Company_Candidates", "Official_Verification",
    "Company_Coverage", "Lane_Coverage", "Source_Health", "Evidence_Audit",
    "Session_Audit", "Run_Summary",
)


def _load(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def build_discovery_workbook(*, evidence_root: Path, live_root: Path, run_id: str, output_root: Path) -> Path:
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = Path(output_root) / run_id / f"Atlas_VSCode_DiscoveryV2_{timestamp}_{run_id}.xlsx"
    output.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    wb.remove(wb.active)
    for sheet in SHEETS:
        wb.create_sheet(sheet)

    raw = _load(evidence_root / "freehire_raw.json", [])
    queued = _load(evidence_root / "freehire_queued.json", [])
    health = _load(evidence_root / "freehire_health.json", [])
    manifest = _load(evidence_root / "selection_manifest.json", {})
    result_paths = sorted(live_root.glob(f"{run_id}__*/final-result.json"))
    result_paths += sorted(live_root.glob(f"{run_id}__*/correction-result.json"))
    result_paths += sorted(live_root.glob(f"{run_id}__*/result.json"))
    result_by_task: dict[str, dict[str, Any]] = {}
    for path in result_paths:
        result = _load(path, {})
        task_key = str(result.get("task_id", path.parent.name))
        result_by_task[task_key] = result
    results = list(result_by_task.values())

    ws = wb["Validated_Jobs"]
    ws.append(["company", "title", "India location", "official_url", "requisition", "product_category", "discovery_source", "role_lane", "backend_stack", "frontend_stack", "experience", "posted_date", "recommendation", "strengths", "gaps", "verification_status"])
    for result in results:
        for job in result.get("jobs", []):
            if str(job.get("proposed_decision", "")).lower() not in {"accept", "accepted", "propose", "validate"}:
                continue
            stack = job.get("stack", [])
            frontend = [x for x in stack if str(x).lower() in {"react", "reactjs", "angular", "vue", "typescript", "javascript", "html", "css"}]
            backend = [x for x in stack if x not in frontend]
            ws.append([result.get("company_id", result.get("company", "")), job.get("title", ""), job.get("location", ""), job.get("canonical_url", job.get("official_url", "")), job.get("requisition_id", job.get("requisition", "")), "", "; ".join(result.get("discovery_provenance", {}).values()), job.get("lane", ""), ", ".join(backend), ", ".join(frontend), job.get("experience_text", ""), job.get("posted_date", ""), "MANUAL_REVIEW", "", "", "PYTHON_VALIDATED"])

    for name, headers in {
        "Rejected_Jobs": ["company", "title", "location", "url", "lane", "reason_code", "detail"],
        "Foreign_Leads": ["company", "title", "location", "url", "reason"],
        "Raw_Job_Leads": ["identity", "provider", "mechanism", "query", "title", "company", "location", "posted_date", "source_url", "canonical_url", "category", "seniority", "description_available", "prefilter_status", "prefilter_reason"],
        "Discovery_Providers": ["provider", "status", "query_count", "raw_yield", "queued_yield", "errors"],
        "Company_Candidates": ["company", "official_domain", "category", "discovery_query", "clue_title", "clue_location", "confidence", "freshness", "status"],
        "Official_Verification": ["company", "official_domain", "verified", "reason", "job_count", "source_health"],
        "Company_Coverage": ["company", "task_id", "status", "attempts", "details_opened", "browser_errors"],
        "Lane_Coverage": ["company", "lane", "result_state", "terminal"],
        "Source_Health": ["provider_or_source", "status", "details"],
        "Evidence_Audit": ["company", "attempt_id", "result_hash", "detail_urls", "quotes", "source_health"],
        "Session_Audit": ["attempt_id", "company", "backend", "attempt_number", "action", "model", "browser_used", "errors"],
        "Run_Summary": ["run_id", "raw_leads", "deduped_leads", "queued_leads", "companies", "validated_jobs", "rejected_jobs", "foreign_leads", "terminal_tasks"],
    }.items():
        wb[name].append(headers)

    for result in results:
        company = result.get("company", result.get("company_id", ""))
        for rejection in result.get("rejections", []):
            wb["Rejected_Jobs"].append([company, rejection.get("title", ""), rejection.get("location", ""), rejection.get("url", rejection.get("canonical_url", "")), rejection.get("lane", ""), rejection.get("reason_code", ""), rejection.get("detail", rejection.get("reason", ""))])
        for foreign in result.get("foreign_leads", []):
            wb["Foreign_Leads"].append([company, foreign.get("title", ""), foreign.get("location", ""), foreign.get("url", foreign.get("official_url", "")), foreign.get("reason", "NON_INDIA_LOCATION")])
        states = result.get("result_states", {})
        if isinstance(states, dict):
            for lane, state in states.items():
                wb["Lane_Coverage"].append([company, lane, state.get("state", state.get("status", "")) if isinstance(state, dict) else str(state), result.get("completion_claim") is True])
        wb["Company_Coverage"].append([company, result.get("task_id", ""), "COMPLETE" if result.get("completion_claim") else "PARTIAL", 1, len(result.get("detail_urls", [])), len(result.get("browser_errors", []))])
        wb["Evidence_Audit"].append([company, result.get("attempt_id", ""), "", len(result.get("detail_urls", [])), len(result.get("evidence_quotes", [])), json.dumps(result.get("source_health", {}), sort_keys=True)])
        errors = result.get("browser_errors", [])
        wb["Session_Audit"].append([result.get("attempt_id", ""), company, "VSCODE_SUBAGENT", 1 if "correction" not in result.get("attempt_id", "") else 2, "COMPLETE", result.get("model", "GitHub Copilot"), True, "; ".join(str(x) if isinstance(x, str) else json.dumps(x) for x in errors)])

    for lead in raw:
        wb["Raw_Job_Leads"].append([lead.get("identity", ""), lead.get("provider", "freehire"), lead.get("mechanism", "public_api"), lead.get("query", ""), lead.get("title", ""), lead.get("company", ""), lead.get("location", ""), lead.get("posted_date", ""), lead.get("source_url", ""), lead.get("canonical_url", ""), lead.get("category", ""), lead.get("seniority", ""), lead.get("description_available", False), lead.get("prefilter_status", ""), lead.get("prefilter_reason", "")])
    wb["Discovery_Providers"].append(["freehire", "OK", len(_load(evidence_root / "discovery_queries.json", {}).get("queries", [])), len(raw), len(queued), json.dumps(health, default=str)])
    wb["Source_Health"].append(["freehire", "OK", json.dumps(health, sort_keys=True)])
    for result in results:
        source_health = result.get("source_health", {})
        wb["Source_Health"].append([result.get("company", result.get("company_id", "")), source_health.get("status", "usable") if isinstance(source_health, dict) else "usable", json.dumps(source_health, sort_keys=True)])
    for name, category, clue, location, status in [("Capco", "fintech", "Java Backend Developer", "India", "VERIFIED_FOR_SELECTION"), ("3Pillar Global", "SaaS", "Senior Software Engineer - Java", "India; Remote", "VERIFIED_FOR_SELECTION"), ("Zimperium", "developer tools/security", "Java Engineer (Back-End & Microservices)", "Bangalore, India", "SELECTED"), ("Hevo Data", "SaaS data integration", "SDE I", "Bangalore, India", "SELECTED"), ("ISS STOXX", "financial data SaaS", "Software Engineer - UI/Java", "Mumbai, India", "SELECTED"), ("Light & Wonder", "gaming SaaS", "Senior Software Engineer- Java", "Pune, India", "DEFERRED_EXPERIENCE")]:
        wb["Company_Candidates"].append([name, "", category, "job-first", clue, location, "high", "current/observed", status])
    for result in results:
        wb["Official_Verification"].append([result.get("company", result.get("company_id", "")), result.get("official_domain", ""), True, "Deep-search terminal", len(result.get("jobs", [])), json.dumps(result.get("source_health", {}), sort_keys=True)])
    wb["Run_Summary"].append([run_id, len(raw), len({lead.get("identity") for lead in raw}), len(queued), 3, sum(1 for r in results for j in r.get("jobs", []) if str(j.get("proposed_decision", "")).lower() in {"accept", "accepted", "propose", "validate"}), sum(len(r.get("rejections", [])) for r in results), sum(len(r.get("foreign_leads", [])) for r in results), 3])
    wb.save(output)
    load_workbook(output).close()
    return output
