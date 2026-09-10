"""Offline tests for the result-handoff boundary + offline recovery patch.

Small, sanitized fixtures derived from the real Copilot event shapes (never the
raw archive or full logs). Covers: event-aware parser extraction, offline
recovery, non-double-counting usage accounting, and generic dialog/modal policy.
No web, no Copilot process, no model.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from atlas.browser_backend.cli_playwright import parse_usage_credits, usage_credits_from_data
from atlas.browser_backend.jsonl_parser import (
    STATE_CONFLICTING, STATE_EVIDENCE_UNFINALIZED, STATE_PROPOSAL_EMITTED,
    STATE_VALIDATED, classify_completion_state, extract_result_objects,
    extract_result_objects_from_events, task_completion_records,
)
from atlas.browser_backend.models import CompanyTask
from atlas.browser_backend.validation import validate_result_object
from atlas.browser_backend import modal_policy as mp
from atlas.browser_backend.recovery import recover

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Sanitized fixtures (shape-faithful, content-synthetic).
# --------------------------------------------------------------------------- #
def _atlas_object(**over) -> dict:
    obj = {
        "atlas_result_version": 1,
        "company": "Acme",
        "task_id": "task-abc",
        "run_id": "run-xyz",
        "official_domain": "acme.com",
        "career_entry_url": "https://careers.acme.com/joblist",
        "route": "cli_playwright",
        "source_family": "custom",
        "status": "SEARCHED_COMPLETE_WITH_MATCHES",
        "observed_result_state": "results_observed",
        "browser_evidence": True,
        "external_block": False,
        "queries_attempted": ["Java Developer India"],
        "lanes_attempted": ["JAVA_BACKEND"],
        "evidence_urls": ["https://careers.acme.com/jobdesc?req=REQ-123"],
        "jobs": [{
            "title": "Java Developer",
            "location": "BANGALORE, India",
            "official_url": "https://careers.acme.com/jobdesc?req=REQ-123",
            "description": "Minimum of 2 years of hands-on experience in Java.",
            "mandatory_requirements": ["Minimum of 2 years of hands-on experience in Java"],
            "preferred_requirements": [],
            "experience_text": "Work Experience of 3 Years to 5 Years",
            "requisition_id": "REQ-123",
            "lane": "JAVA_BACKEND",
            "evidence_snippets": ["Skills: Technology->Java->Springboot"],
            "posted_date": "",
            "source_family": "custom",
        }],
        "rejections": [],
        "limitations": [],
    }
    obj.update(over)
    return obj


def _fenced(obj: dict) -> str:
    return "Search complete.\n```json\n" + json.dumps(obj) + "\n```\n"


def _ev_toolrequest(obj):
    return {"type": "assistant.message",
            "data": {"content": "",
                     "toolRequests": [{"toolCallId": "tc1", "name": "task_complete",
                                       "arguments": {"summary": _fenced(obj)}}]}}


def _ev_exec_start(obj):
    return {"type": "tool.execution_start",
            "data": {"toolCallId": "tc1", "toolName": "task_complete",
                     "arguments": {"summary": _fenced(obj)}}}


def _ev_exec_complete(obj, success=True):
    return {"type": "tool.execution_complete",
            "data": {"toolCallId": "tc1", "success": success,
                     "result": {"content": _fenced(obj), "detailedContent": _fenced(obj)}}}


def _ev_session_tc(obj, success=True):
    return {"type": "session.task_complete", "data": {"summary": _fenced(obj), "success": success}}


def _ev_mcp_tool():
    return {"type": "tool.execution_start", "data": {"toolCallId": "b1", "toolName": "playwright_navigate"}}


# --------------------------------------------------------------------------- #
# PARSER — event-aware extraction
# --------------------------------------------------------------------------- #
def test_empty_assistant_content_plus_toolrequest_extracts_object():
    obj = _atlas_object()
    events = [{"type": "assistant.message", "data": {"content": "stall note"}}, _ev_toolrequest(obj)]
    ex = extract_result_objects_from_events(events)
    assert ex["primary"] is not None
    assert ex["primary"]["run_id"] == "run-xyz"
    assert not ex["conflict"]


def test_tool_execution_start_summary_extracts_object():
    obj = _atlas_object()
    ex = extract_result_objects_from_events([_ev_exec_start(obj)])
    assert ex["primary"] == obj


def test_tool_execution_complete_result_content_extracts_object():
    obj = _atlas_object()
    ex = extract_result_objects_from_events([_ev_exec_complete(obj)])
    assert ex["primary"] == obj


def test_session_task_complete_summary_extracts_object():
    obj = _atlas_object()
    ex = extract_result_objects_from_events([_ev_session_tc(obj)])
    assert ex["primary"] == obj


def test_repeated_same_object_dedups_to_one():
    obj = _atlas_object()
    events = [_ev_toolrequest(obj), _ev_exec_start(obj), _ev_exec_complete(obj), _ev_session_tc(obj)]
    ex = extract_result_objects_from_events(events)
    assert len(ex["objects"]) == 1
    # provenance keeps all sources for the single object.
    prov = list(ex["provenance"].values())[0]
    sources = {p["source"] for p in prov}
    assert {"assistant.message.toolRequest", "tool.execution_start",
            "tool.execution_complete", "session.task_complete"} <= sources


def test_first_attempt_no_object_is_unfinalized():
    events = [{"type": "assistant.message", "data": {"content": "closed the dialog, searched"}},
              _ev_mcp_tool()]
    ex = extract_result_objects_from_events(events)
    assert ex["primary"] is None and not ex["conflict"]
    assert classify_completion_state(events) == STATE_EVIDENCE_UNFINALIZED


def test_conflicting_objects_rejected():
    a = _atlas_object()
    b = _atlas_object(status="SEARCHED_COMPLETE_NO_MATCHES", jobs=[])
    ex = extract_result_objects_from_events([_ev_exec_start(a), _ev_session_tc(b)])
    assert ex["conflict"] is True
    assert ex["primary"] is None
    assert classify_completion_state([_ev_exec_start(a), _ev_session_tc(b)]) == STATE_CONFLICTING


def test_ids_must_match_downstream():
    obj = _atlas_object(task_id="WRONG", run_id="WRONG")
    task = CompanyTask(company="Acme", task_id="task-abc", run_id="run-xyz")
    contract = validate_result_object(obj, task)
    assert any("TASK_ID_MISMATCH" in f for f in contract["failures"])
    assert any("RUN_ID_MISMATCH" in f for f in contract["failures"])


def test_existing_assistant_content_extraction_still_works():
    obj = _atlas_object()
    assert extract_result_objects(_fenced(obj)) == [obj]


def test_completion_state_proposal_vs_validated():
    obj = _atlas_object()
    events = [_ev_mcp_tool(), _ev_session_tc(obj)]
    assert classify_completion_state(events, validated=False) == STATE_PROPOSAL_EMITTED
    assert classify_completion_state(events, validated=True) == STATE_VALIDATED


def test_task_completion_records_preserve_provenance():
    obj = _atlas_object()
    recs = task_completion_records([_ev_session_tc(obj)])
    assert recs and recs[0]["source"] == "session.task_complete"
    assert recs[0]["has_result_object"] is True


# --------------------------------------------------------------------------- #
# USAGE — no double counting
# --------------------------------------------------------------------------- #
def test_top_level_total_used_once_second_attempt():
    data = {"totalNanoAiu": 71237430000,
            "modelMetrics": {"m": {"totalNanoAiu": 71237430000}},
            "agentMetrics": {"main": {"totalNanoAiu": 70398280000},
                             "sub": {"totalNanoAiu": 839150000}}}
    assert round(usage_credits_from_data(data), 5) == 71.23743


def test_top_level_total_used_once_first_attempt():
    data = {"totalNanoAiu": 82217400000,
            "modelMetrics": {"m": {"totalNanoAiu": 82217400000}}}
    assert round(usage_credits_from_data(data), 4) == 82.2174


def test_usage_fallback_single_child_when_top_level_absent():
    data = {"modelMetrics": {"a": {"totalNanoAiu": 5000000000},
                             "b": {"totalNanoAiu": 3000000000}}}
    # A single child aggregate is used (the largest), never the sum (8.0).
    assert usage_credits_from_data(data) == 5.0


def test_usage_top_level_credit_field():
    assert usage_credits_from_data({"ai_credits": 12.5}) == 12.5


def test_parse_usage_credits_file(tmp_path):
    p = tmp_path / "u.json"
    p.write_text(json.dumps({"totalNanoAiu": 82217400000}), encoding="utf-8")
    assert parse_usage_credits(p) == 82.2174


# --------------------------------------------------------------------------- #
# MODAL — generic dialog policy
# --------------------------------------------------------------------------- #
_HTML_MODAL = (
    'dialog [role=dialog] [ref=d1]:\n'
    '  - mat-dialog-title [ref=t1]: Important Notice\n'
    '  - generic: This website uses cookies. Beware of recruitment fraud.\n'
    '  - cdk-overlay-backdrop\n'
    '  - button "Close" [ref=b1]\n'
    '  - button "Ok" [ref=b2]\n'
    '  note: overlay intercepts pointer events for Find Jobs\n'
)


def test_important_notice_html_modal_classified():
    obs = mp.classify_dialog(_HTML_MODAL, ["Close", "Ok"], title="Important Notice")
    assert obs.kind == mp.KIND_HTML_MODAL
    assert obs.role_dialog and obs.has_overlay and obs.click_intercepted
    assert obs.informational


def test_html_modal_safe_close_and_resnapshot():
    obs = mp.classify_dialog(_HTML_MODAL, ["Close", "Ok"], title="Important Notice")
    act = mp.decide_dialog_action(obs)
    assert act.disposition == mp.DISPOSITION_SAFE_DISMISS
    assert act.button.lower() == "close"
    assert act.requires_resnapshot is True


def test_unsafe_buttons_excluded_and_block_is_internal():
    frag = ('dialog [role=dialog] [ref=d2]:\n  - generic: Please sign in to continue\n'
            '  - button "Sign In" [ref=b1]\n  - button "Register" [ref=b2]\n')
    obs = mp.classify_dialog(frag, ["Sign In", "Register"])
    assert obs.unsafe_buttons and not obs.safe_buttons
    act = mp.decide_dialog_action(obs)
    assert act.disposition == mp.DISPOSITION_UNSAFE_BLOCK
    assert act.policy_label == mp.INTERNAL_INTERACTION_BLOCKED
    # Never an employer/external access block.
    from atlas.pilot.status_v4 import EXTERNAL_BLOCKER
    assert act.status_hint not in EXTERNAL_BLOCKER


def test_native_and_html_dialog_kept_distinct():
    native = mp.classify_dialog("Browser JavaScript dialog: browser_handle_dialog type: confirm", [])
    assert native.kind == mp.KIND_NATIVE_JS
    html = mp.classify_dialog(_HTML_MODAL, ["Close", "Ok"])
    assert html.kind == mp.KIND_HTML_MODAL
    assert native.kind != html.kind


# --------------------------------------------------------------------------- #
# RECOVERY — offline reconstruction
# --------------------------------------------------------------------------- #
_SNAPSHOT = (
    '- generic [ref=f1]: Java Developer\n'
    '- generic [ref=f2]: BANGALORE, Infosys Limited\n'
    '- generic [ref=f3]: Job ID/Reference Code\n'
    '- generic [ref=f4]: REQ-123\n'
    '- strong [ref=f5]: Responsibilities\n'
    '- generic [ref=f6]: Minimum of 2 years of hands-on experience in Java '
    '• Strong experience in Spring Framework such as Spring MVC, IOC, AOP '
    '• Work Experience of 3 Years to 5 Years\n'
    '- generic [ref=f7]: Technology->Java->Spring->Spring\n'
    '- generic [ref=f8]: Technology->Java->Springboot\n'
)


def _make_run(tmp_path: Path) -> Path:
    run = tmp_path / "canary-run-1"
    cdir = run / "Acme-1234"
    (cdir / "mcp-output").mkdir(parents=True)
    obj = _atlas_object()
    events = [_ev_mcp_tool(), _ev_toolrequest(obj), _ev_exec_start(obj),
              _ev_exec_complete(obj), _ev_session_tc(obj)]
    (cdir / "copilot_events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events), encoding="utf-8")
    (cdir / "copilot_usage.json").write_text(
        json.dumps({"totalNanoAiu": 71237430000,
                    "modelMetrics": {"m": {"totalNanoAiu": 71237430000}}}), encoding="utf-8")
    (cdir / "backend_result.json").write_text(
        json.dumps({"status": "PARSER_ERROR", "usage_credits": 284.9497}), encoding="utf-8")
    (cdir / "mcp-output" / "page-2026-01-01T00-00-00.yml").write_text(_SNAPSHOT, encoding="utf-8")
    return run


def _dir_hashes(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def test_recovery_validates_and_preserves_source(tmp_path):
    run = _make_run(tmp_path)
    before = _dir_hashes(run)
    out = recover(run, write_workbook=True, git_commit="deadbeef",
                  output_root=tmp_path / "recoveries")
    after = _dir_hashes(run)
    assert before == after                         # source unchanged
    assert out.status == "SEARCHED_COMPLETE_WITH_MATCHES"
    assert out.valid is True
    assert out.completion_state == STATE_VALIDATED
    assert out.usage_credits == 71.23743           # corrected, single-count
    assert out.original_usage_credits == 284.9497
    # Unique immutable child.
    assert Path(out.recovery_dir).exists()
    manifest = json.loads((Path(out.recovery_dir) / "recovery_manifest.json").read_text())
    assert manifest["no_network"] and manifest["no_model"] and manifest["no_browser"]
    assert manifest["selected_snapshot"].endswith(".yml")   # snapshot selected


def test_recovery_grounds_quotes_in_exact_source(tmp_path):
    run = _make_run(tmp_path)
    out = recover(run, output_root=tmp_path / "recoveries")
    validated = json.loads((Path(out.recovery_dir) / "validated_result.json").read_text())
    assert validated["jobs"], "expected one accepted, grounded job"
    quotes = validated["jobs"][0]["evidence_snippets"]
    corpus = (validated["jobs"][0]["description"] + " "
              + " ".join(validated["jobs"][0]["preferred_requirements"])).lower()
    # Every accepted quote is an exact substring of the captured source text.
    assert quotes and all(q.lower() in corpus for q in quotes)
    assert any("Technology->Java->Springboot" == q for q in quotes)


def test_recovery_workbook_reopens_five_sheets(tmp_path):
    import openpyxl
    run = _make_run(tmp_path)
    out = recover(run, write_workbook=True, output_root=tmp_path / "recoveries")
    wb = openpyxl.load_workbook(out.workbook_path)
    assert wb.sheetnames == ["Validated_Jobs", "Rejected_Jobs", "Company_Coverage",
                             "Evidence_Audit", "Usage"]


def test_recovery_idempotent_and_resolution_log_append_only(tmp_path):
    run = _make_run(tmp_path)
    root = tmp_path / "recoveries"
    out1 = recover(run, write_workbook=True, output_root=root)
    manifest_bytes = (Path(out1.recovery_dir) / "recovery_manifest.json").read_bytes()
    out2 = recover(run, write_workbook=True, output_root=root)
    assert out2.reused_existing is True
    assert out2.recovery_run_id == out1.recovery_run_id
    # Not overwritten.
    assert (Path(out1.recovery_dir) / "recovery_manifest.json").read_bytes() == manifest_bytes
    # Append-only resolution log grew by exactly one line (only the first run wrote).
    log = root / "resolution.log.jsonl"
    assert log.exists()
    assert len([ln for ln in log.read_text().splitlines() if ln.strip()]) == 1


def test_recovery_selects_snapshot_by_requisition(tmp_path):
    run = _make_run(tmp_path)
    # Add a decoy snapshot with no canonical anchor.
    (run / "Acme-1234" / "mcp-output" / "page-decoy.yml").write_text(
        "- generic [ref=x]: unrelated listing page\n", encoding="utf-8")
    out = recover(run, output_root=tmp_path / "recoveries")
    manifest = json.loads((Path(out.recovery_dir) / "recovery_manifest.json").read_text())
    assert "page-2026-01-01T00-00-00.yml" in manifest["selected_snapshot"]
