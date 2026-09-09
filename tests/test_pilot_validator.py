"""Failing-first product-validator tests (prompt s.14, audit 3.10).

The validator must fail on real usefulness violations, not just workbook shape.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from atlas.pilot.config import load_pilot_config
from atlas.pilot.validator import validate_history, validate_pilot_run


@pytest.fixture(scope="module")
def config():
    return load_pilot_config()


def _write_run(tmp_path, *, accepted, synthetic=False, companies=10, statuses="COMPLETE"):
    run_dir = tmp_path / "llm_pilots" / "R1"
    run_dir.mkdir(parents=True)
    (run_dir / "Atlas_LLM_India_Pilot_x_R1.xlsx").write_bytes(b"x")
    coverage = {"companies": [
        {"company": f"C{i}", "status": statuses, "route": "ATS_GREENHOUSE",
         "source_family": "OFFICIAL_ATS_GREENHOUSE", "official_domain": f"c{i}.example",
         "career_entry_url": f"https://c{i}.example/careers",
         "lanes": {l: {"attempted": True, "board_snapshot_evaluated": True, "queries": [], "pages": 1,
                       "candidates": 0} for l in config_lanes()}, "model": "claude-sonnet-5",
         "tool_calls": 5, "queries_attempted": [], "pages_or_interactions": 2, "limitations": []}
        for i in range(companies)
    ]}
    (run_dir / "coverage.json").write_text(json.dumps(coverage))
    (run_dir / "jobs_accepted.json").write_text(json.dumps(accepted))
    (run_dir / "llm_usage.json").write_text(json.dumps({
        "totals": {"input_tokens": 10, "output_tokens": 5}, "by_model": {"claude-sonnet-5": {
            "input_tokens": 10, "output_tokens": 5}}}))
    (run_dir / "run_manifest.json").write_text(json.dumps({
        "run_id": "R1", "candidate_provenance": {"synthetic": synthetic}}))
    return run_dir


def config_lanes():
    return ("JAVA_BACKEND", "JAVA_FULLSTACK", "REACT_FRONTEND", "DOTNET",
            "ENTERPRISE_HR_PAYROLL_INTEGRATION")


def _india_row(**kw):
    row = {"company": "C0", "title": "Java Backend Engineer", "lane": "JAVA_BACKEND",
           "geography_decision": "INDIA_ELIGIBLE", "geography_class": "INDIA_PRIMARY",
           "unsupported_mandatory_backend": "",
           "supported_stack_evidence": "Java, Spring Boot", "requirement_evidence": "matched: Java",
           "location_evidence": "INDIA_PRIMARY: Bengaluru, India", "recommendation": "STRONG_APPLY"}
    row.update(kw)
    return row


def test_clean_run_passes(tmp_path, config):
    run_dir = _write_run(tmp_path, accepted=[_india_row()])
    rep = validate_pilot_run(run_dir, config)
    assert rep.passed, rep.failures


def test_india_eligible_audit_value_passes(tmp_path, config):
    # Section 13: the audit column value INDIA_ELIGIBLE is the accepted geography verdict.
    run_dir = _write_run(tmp_path, accepted=[_india_row(geography_decision="INDIA_ELIGIBLE")])
    rep = validate_pilot_run(run_dir, config)
    assert rep.passed, rep.failures


def test_foreign_row_fails(tmp_path, config):
    run_dir = _write_run(tmp_path, accepted=[_india_row(geography_decision="FOREIGN_EXCLUDED")])
    rep = validate_pilot_run(run_dir, config)
    assert not rep.passed
    assert any("india_only" in c["check"] for c in rep.failures)


def test_unknown_location_row_fails(tmp_path, config):
    run_dir = _write_run(tmp_path, accepted=[_india_row(geography_decision="UNKNOWN_LOCATION")])
    rep = validate_pilot_run(run_dir, config)
    assert not rep.passed


def test_unsupported_backend_row_fails(tmp_path, config):
    run_dir = _write_run(tmp_path, accepted=[_india_row(unsupported_mandatory_backend="Node.js")])
    rep = validate_pilot_run(run_dir, config)
    assert not rep.passed
    assert any("no_unsupported_backend" in c["check"] for c in rep.failures)


def test_synthetic_candidate_fails(tmp_path, config):
    run_dir = _write_run(tmp_path, accepted=[_india_row()], synthetic=True)
    rep = validate_pilot_run(run_dir, config)
    assert not rep.passed
    assert any("real_candidate_profile" in c["check"] for c in rep.failures)


def test_fewer_than_ten_companies_fails(tmp_path, config):
    run_dir = _write_run(tmp_path, accepted=[_india_row()], companies=3)
    rep = validate_pilot_run(run_dir, config)
    assert not rep.passed
    assert any("all_companies_terminal" in c["check"] for c in rep.failures)


def test_zero_accept_is_allowed_without_padding(tmp_path, config):
    run_dir = _write_run(tmp_path, accepted=[])
    rep = validate_pilot_run(run_dir, config)
    assert rep.passed  # zero accepted rows is valid (no padding)


def test_history_gate_detects_change(tmp_path):
    # a prior artifact that we then modify must be detected as changed
    prior_file = tmp_path / "prior.txt"
    prior_file.write_text("original")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps([{
        "path": "prior.txt", "len": 8,
        "sha256": hashlib.sha256(b"original").hexdigest(),
    }]))
    rep_ok = validate_history(manifest, tmp_path)
    assert rep_ok.passed
    prior_file.write_text("TAMPERED")
    rep_bad = validate_history(manifest, tmp_path)
    assert not rep_bad.passed
    assert any("prior_runs_unchanged" in c["check"] for c in rep_bad.failures)
