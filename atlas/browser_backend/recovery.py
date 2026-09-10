"""Offline recovery of a completed company run from its archived artifacts.

Reconstructs a validated Atlas result from an existing canary/company run
*without* re-browsing, re-running Copilot, hitting the network, or calling a
model. It:

1. reads the archived Copilot JSONL + Playwright snapshots (never mutating them);
2. extracts the Atlas object from the authoritative ``task_complete`` completion
   events (event-aware parser);
3. grounds it in the exact archived job-detail snapshot text;
4. runs the ordinary deterministic validators (no forced acceptance);
5. writes a unique, immutable child recovery run with full provenance, corrected
   usage, and an optional five-sheet workbook;
6. appends an entry to an append-only resolution log.

The original run's files are preserved byte-for-byte.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from atlas.browser_backend.cli_playwright import usage_credits_from_data
from atlas.browser_backend.evidence_reconstruction import (
    extract_source_evidence, find_detail_snapshot, ground_job,
)
from atlas.browser_backend.jsonl_parser import (
    PARSER_VERSION, classify_completion_state, extract_result_objects_from_events,
    load_events, task_completion_records,
)
from atlas.browser_backend.models import CompanyTask
from atlas.browser_backend.validation import build_validated_result, validate_result_object
from atlas.pilot.status_v4 import CompanySearchStatus

RECOVERY_ROOT = Path("output") / "production" / "browser_backend_recoveries"
RESOLUTION_LOG = Path("output") / "production" / "browser_backend_recovery_resolution.log.jsonl"

_RECOVERY_FILES = (
    "recovery_manifest.json", "source_event_provenance.json", "recovered_proposal.json",
    "validated_result.json", "recovery_validation.json", "usage_corrected.json",
)


@dataclass
class RecoveryOutcome:
    recovery_run_id: str = ""
    recovery_dir: str = ""
    parent_run_dir: str = ""
    parent_run_id: str = ""
    status: str = ""
    valid: bool = False
    completion_state: str = ""
    workbook_path: str = ""
    usage_credits: float = 0.0
    original_usage_credits: float = 0.0
    reused_existing: bool = False
    validation_failures: tuple[str, ...] = ()
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "recovery_run_id": self.recovery_run_id,
            "recovery_dir": self.recovery_dir,
            "parent_run_dir": self.parent_run_dir,
            "parent_run_id": self.parent_run_id,
            "status": self.status,
            "valid": self.valid,
            "completion_state": self.completion_state,
            "workbook_path": self.workbook_path,
            "usage_credits": self.usage_credits,
            "original_usage_credits": self.original_usage_credits,
            "reused_existing": self.reused_existing,
            "validation_failures": list(self.validation_failures),
            "error": self.error,
        }


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _find_company_dir(run_dir: Path) -> Path:
    """The inner company dir that holds ``copilot_events.jsonl``."""
    if (run_dir / "copilot_events.jsonl").exists():
        return run_dir
    for cand in sorted(run_dir.glob("*/copilot_events.jsonl")):
        return cand.parent
    return run_dir


def _hash_inputs(company_dir: Path) -> dict:
    files = ["copilot_events.jsonl", "copilot_usage.json", "backend_result.json"]
    hashes: dict[str, str] = {}
    for name in files:
        p = company_dir / name
        if p.exists():
            hashes[name] = _sha256_file(p)
    mcp = company_dir / "mcp-output"
    if mcp.exists():
        for snap in sorted(mcp.glob("page-*.yml")):
            hashes[f"mcp-output/{snap.name}"] = _sha256_file(snap)
    return hashes


def _recovery_run_id(parent_run_id: str, events_hash: str) -> str:
    return f"recovery-{parent_run_id}-{(events_hash or '0')[:8]}"


def _timestamp_for(parent_run_id: str) -> str:
    # Reuse the parent run's embedded timestamp when present for byte-stable,
    # idempotent output; else derive one now.
    import re
    m = re.search(r"(\d{8}-\d{6})", parent_run_id or "")
    return m.group(1) if m else time.strftime("%Y%m%d-%H%M%S")


def recover(run_dir: Path, *, write_workbook: bool = False, git_commit: str = "",
            output_root: Optional[Path] = None, force: bool = False) -> RecoveryOutcome:
    run_dir = Path(run_dir)
    company_dir = _find_company_dir(run_dir)
    events_path = company_dir / "copilot_events.jsonl"
    if not events_path.exists():
        return RecoveryOutcome(parent_run_dir=str(run_dir), status="PARSER_ERROR",
                               error=f"no copilot_events.jsonl under {run_dir}")

    parent_run_id = run_dir.name
    input_hashes = _hash_inputs(company_dir)
    events_hash = input_hashes.get("copilot_events.jsonl", "")
    recovery_run_id = _recovery_run_id(parent_run_id, events_hash)
    root = Path(output_root) if output_root else RECOVERY_ROOT
    recovery_dir = (root / recovery_run_id).resolve()

    manifest_path = recovery_dir / "recovery_manifest.json"
    if manifest_path.exists() and not force:
        # Idempotent: a completed recovery for this exact source already exists.
        try:
            prior = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            prior = {}
        return RecoveryOutcome(
            recovery_run_id=recovery_run_id, recovery_dir=str(recovery_dir),
            parent_run_dir=str(run_dir), parent_run_id=parent_run_id,
            status=prior.get("status", ""), valid=bool(prior.get("valid")),
            completion_state=prior.get("completion_state", ""),
            workbook_path=prior.get("workbook_path", ""),
            usage_credits=float(prior.get("usage", {}).get("corrected_credits", 0.0)),
            original_usage_credits=float(prior.get("usage", {}).get("original_recorded_credits", 0.0)),
            reused_existing=True,
            validation_failures=tuple(prior.get("validation_failures", [])),
        )

    events = load_events(events_path)
    extraction = extract_result_objects_from_events(events)
    provenance_records = task_completion_records(events)

    if extraction["conflict"]:
        return _write_failed(
            recovery_dir, recovery_run_id, run_dir, parent_run_id, company_dir,
            input_hashes, git_commit, extraction, provenance_records,
            state=classify_completion_state(events),
            status="PARSER_ERROR", error="conflicting result objects across completion events")
    proposed = extraction["primary"]
    if proposed is None:
        return _write_failed(
            recovery_dir, recovery_run_id, run_dir, parent_run_id, company_dir,
            input_hashes, git_commit, extraction, provenance_records,
            state=classify_completion_state(events),
            status="PARSER_ERROR", error="no machine-readable result object in completion events")

    # Rebuild the task from the proposed object so validation IDs match the run.
    task = CompanyTask(
        company=str(proposed.get("company", "")),
        official_domain=str(proposed.get("official_domain", "")),
        career_entry_url=str(proposed.get("career_entry_url", "")),
        run_id=str(proposed.get("run_id", parent_run_id)),
        task_id=str(proposed.get("task_id", "")) or "recovered",
        lanes=tuple(str(x) for x in (proposed.get("lanes_attempted") or ())),
    )

    # Ground each job in the exact archived detail snapshot.
    grounded = dict(proposed)
    grounded_jobs = []
    selected_snapshot = ""
    source_evidence_dump = []
    for job in (proposed.get("jobs") or []):
        if not isinstance(job, dict):
            continue
        req = str(job.get("requisition_id", ""))
        url = str(job.get("official_url", "") or job.get("url", ""))
        title = str(job.get("title", ""))
        snap = find_detail_snapshot(company_dir, requisition_id=req, url=url, title=title)
        if snap is not None:
            selected_snapshot = str(snap)
            ev = extract_source_evidence(
                snap.read_text(encoding="utf-8", errors="replace"),
                requisition_id=req, title=title, snapshot_path=str(snap))
            ev.matched_url = url in snap.read_text(encoding="utf-8", errors="replace") if url else False
            source_evidence_dump.append(ev.to_dict())
            grounded_jobs.append(ground_job(job, ev))
        else:
            grounded_jobs.append(dict(job))
    grounded["jobs"] = grounded_jobs

    # Deterministic validation (never forced).
    contract = validate_result_object(grounded, task, custom_site=True)
    validated_result = None
    status = CompanySearchStatus.PARSER_ERROR.value
    valid = False
    failures = list(contract["failures"])
    if not failures:
        validated_result = build_validated_result(grounded, task)
        status = validated_result.status
        # A with-matches status that produced zero accepted jobs is a truthful
        # PARTIAL/REJECTED downgrade, already applied by build_validated_result.
        valid = status in ("SEARCHED_COMPLETE_WITH_MATCHES", "SEARCHED_COMPLETE_NO_MATCHES")

    # Corrected usage (no double counting).
    usage_path = company_dir / "copilot_usage.json"
    corrected = 0.0
    if usage_path.exists():
        try:
            corrected = round(usage_credits_from_data(
                json.loads(usage_path.read_text(encoding="utf-8", errors="replace"))), 5)
        except (ValueError, OSError):
            corrected = 0.0
    original_recorded = _original_recorded_usage(company_dir)

    completion_state = classify_completion_state(events, validated=valid)

    # --- write the immutable child recovery run ---
    recovery_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "recovery_run_id": recovery_run_id,
        "parent_run_id": parent_run_id,
        "parent_run_dir": str(run_dir),
        "parent_company_dir": str(company_dir),
        "parser_version": PARSER_VERSION,
        "git_commit": git_commit,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "no_network": True,
        "no_model": True,
        "no_browser": True,
        "source_input_hashes": input_hashes,
        "selected_completion_events": [
            r for r in provenance_records if r.get("has_result_object")],
        "selected_snapshot": selected_snapshot,
        "completion_state": completion_state,
        "status": status,
        "valid": valid,
        "validation_failures": failures,
        "usage": {
            "corrected_credits": corrected,
            "original_recorded_credits": original_recorded,
            "double_count_factor": round(original_recorded / corrected, 4) if corrected else None,
        },
        "workbook_path": "",
    }
    _write_json(recovery_dir / "source_event_provenance.json", {
        "candidates": extraction["candidates"],
        "task_completion_records": provenance_records,
        "object_provenance": [
            {"sources": v} for v in extraction["provenance"].values()],
        "source_evidence": source_evidence_dump,
    })
    _write_json(recovery_dir / "recovered_proposal.json", grounded)
    _write_json(recovery_dir / "validated_result.json",
                validated_result.to_dict() if validated_result is not None else None)
    _write_json(recovery_dir / "recovery_validation.json", {
        "contract_failures": contract["failures"],
        "browser_evidence": contract["browser_evidence"],
        "external_block": contract["external_block"],
        "status": status,
        "valid": valid,
        "completion_state": completion_state,
    })
    _write_json(recovery_dir / "usage_corrected.json", {
        "corrected_credits": corrected,
        "original_recorded_credits": original_recorded,
        "precedence": "top_level_totalNanoAiu/1e9 -> top_level_credit -> single_child_aggregate",
        "note": "parent and child aggregates are never summed",
    })

    workbook_path = ""
    if write_workbook:
        from atlas.browser_backend.recovery_report import write_recovery_workbook
        ts = _timestamp_for(parent_run_id)
        wb_path = recovery_dir / f"Atlas_Browser_Recovery_{ts}_{recovery_run_id}.xlsx"
        workbook_path = str(write_recovery_workbook(
            wb_path, manifest=manifest, proposed=grounded,
            validated=(validated_result.to_dict() if validated_result is not None else None),
            contract=contract, source_evidence=source_evidence_dump,
            original_recorded_credits=original_recorded, corrected_credits=corrected))
        manifest["workbook_path"] = workbook_path

    _write_json(manifest_path, manifest)

    _append_resolution(RESOLUTION_LOG if output_root is None else (root / "resolution.log.jsonl"), {
        "resolved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "parent_run_id": parent_run_id,
        "recovery_run_id": recovery_run_id,
        "recovery_dir": str(recovery_dir),
        "status": status,
        "valid": valid,
        "completion_state": completion_state,
        "corrected_credits": corrected,
        "original_recorded_credits": original_recorded,
        "workbook_path": workbook_path,
        "original_status_retained": True,
    })

    return RecoveryOutcome(
        recovery_run_id=recovery_run_id, recovery_dir=str(recovery_dir),
        parent_run_dir=str(run_dir), parent_run_id=parent_run_id, status=status,
        valid=valid, completion_state=completion_state, workbook_path=workbook_path,
        usage_credits=corrected, original_usage_credits=original_recorded,
        validation_failures=tuple(failures))


def _original_recorded_usage(company_dir: Path) -> float:
    br = company_dir / "backend_result.json"
    if br.exists():
        try:
            return float(json.loads(br.read_text(encoding="utf-8")).get("usage_credits", 0.0))
        except (ValueError, OSError):
            return 0.0
    return 0.0


def _write_failed(recovery_dir, recovery_run_id, run_dir, parent_run_id, company_dir,
                  input_hashes, git_commit, extraction, provenance_records, *,
                  state, status, error) -> RecoveryOutcome:
    recovery_dir = Path(recovery_dir)
    recovery_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "recovery_run_id": recovery_run_id, "parent_run_id": parent_run_id,
        "parent_run_dir": str(run_dir), "parser_version": PARSER_VERSION,
        "git_commit": git_commit,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "no_network": True, "no_model": True, "no_browser": True,
        "source_input_hashes": input_hashes, "completion_state": state,
        "status": status, "valid": False, "error": error,
        "validation_failures": [],
    }
    _write_json(recovery_dir / "recovery_manifest.json", manifest)
    _write_json(recovery_dir / "source_event_provenance.json", {
        "candidates": extraction["candidates"],
        "task_completion_records": provenance_records,
    })
    _write_json(recovery_dir / "recovered_proposal.json",
                extraction["objects"] if extraction["objects"] else None)
    return RecoveryOutcome(
        recovery_run_id=recovery_run_id, recovery_dir=str(recovery_dir),
        parent_run_dir=str(run_dir), parent_run_id=parent_run_id, status=status,
        valid=False, completion_state=state, error=error)


def _write_json(path: Path, obj) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8"))


def _append_resolution(path: Path, row: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


__all__ = ["RECOVERY_ROOT", "RESOLUTION_LOG", "RecoveryOutcome", "recover"]
