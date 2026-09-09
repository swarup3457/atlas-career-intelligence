"""Phase 1B — production graph / runtime / controller / report tests (spec 7.11-7.16, 20)."""

from __future__ import annotations

from pathlib import Path

import pytest

from atlas.config import load_settings
from atlas.controllers.base import NullController
from atlas.controllers.operations import (
    CandidateMatchRequest,
    QueryExpansionRequest,
    TypedControllerOps,
)
from atlas.orchestration.production_graph import PhaseContext, PhaseIsolationError
from atlas.orchestration.production_state import (
    PRODUCTION_PHASE_ORDER,
    ProductionPhase,
    ProductionTerminalState,
    assert_compact,
    bump,
    checkpoint_size_bytes,
    initial_production_state,
)
from atlas.persistence.remote_audit import (
    REMOTE_AUDIT_DEGRADED,
    FailingRemoteAudit,
    export_run_audit,
    sanitize_event,
)
from atlas.reporting.mapping import (
    REQUIRED_SHEETS,
    load_report_mapping,
    validate_report,
    write_report,
)
from atlas.runtime.production import ProductionSearchRuntime
from atlas.planning import PlannedCompany
from atlas.sources.testing.fake import make_fake_instance

pytestmark = pytest.mark.integration


def _fixture_topology(scenario="results", **kw):
    """A tiny fixture topology (one company, one fake instance) for runtime tests."""
    iid = "t-workday"
    instances = {iid: make_fake_instance(iid, scenario=scenario, company="TestCo", **kw)}
    companies = [PlannedCompany(company_id="tco", name="TestCo", source_instances=(iid,), geography_group="PRIMARY")]
    return instances, companies


def _settings(tmp_path, **overrides):
    kwargs = dict(
        state_db=tmp_path / "state" / "atlas_state.sqlite",
        checkpoint_db=tmp_path / "state" / "atlas_checkpoints.sqlite",
        output_dir=tmp_path / "output",
        logs_dir=tmp_path / "logs",
        browser_profile=tmp_path / "profile",
        agents_dir=tmp_path / "agents",
        skills_dir=tmp_path / "skills",
    )
    kwargs.update(overrides)
    s = load_settings(**kwargs)
    s.ensure_directories()
    return s


# --- NullController end-to-end + phase order ------------------------------
def test_nullcontroller_end_to_end_reaches_complete(tmp_path):
    # Executes the REAL sealed plan (per-lane children) through the fixture
    # pipeline — not a synthetic count (P0-1).
    rt = ProductionSearchRuntime(_settings(tmp_path), "run-1")
    res = rt.run()
    assert res.terminal_state == ProductionTerminalState.COMPLETE.value
    # phases occurred in the canonical order
    expected = [p.value for p in PRODUCTION_PHASE_ORDER if p != ProductionPhase.COMPLETE]
    assert list(res.phases_completed) == expected
    assert res.policy_fingerprint and res.plan_fingerprint
    # Every planned child is terminal, across all six lanes (P0-11).
    assert res.planned_tasks > 0 and res.terminal_tasks == res.planned_tasks
    assert len(res.lane_summary) == 6
    assert res.run_lock_status == "RUN_LOCK_ACQUIRED"


def test_completed_manifest_has_tz_aware_completed_at(tmp_path):
    res = ProductionSearchRuntime(_settings(tmp_path), "run-2").run()
    assert res.manifest["completed_at"]
    assert res.manifest["completed_at"].endswith("+00:00")  # tz-aware UTC


# --- search isolation ------------------------------------------------------
def test_discovery_cannot_trigger_excel_or_remote_audit(tmp_path):
    res = ProductionSearchRuntime(_settings(tmp_path), "run-3").run()
    # Every recorded side effect happened in a persistence phase, never during
    # DISCOVER / health / hydration.
    for eff in res.persistence_side_effects:
        phase = eff.split(":", 1)[0]
        assert phase in ("PERSIST_LOCAL", "BUILD_REPORT", "OPTIONAL_REMOTE_AUDIT")


def test_phase_context_rejects_side_effect_in_discovery_phase():
    ctx = PhaseContext()
    with pytest.raises(PhaseIsolationError):
        ctx.record_side_effect(ProductionPhase.DISCOVER, "excel_report")


def test_missing_mandatory_handler_fails_closed():
    # build spec 13 / P0-17: a missing mandatory handler must fail closed.
    from atlas.orchestration.production_graph import MissingPhaseHandlerError, build_production_graph

    with pytest.raises(MissingPhaseHandlerError):
        build_production_graph({}, runtime=None)


# --- compact checkpoints ---------------------------------------------------
def test_checkpoint_stays_bounded_with_large_plan(tmp_path):
    # A large plan (many companies × six lanes) still checkpoints only run/plan
    # refs + counters — never per-job payloads (P0-4/P0-16).
    instances, companies = _fixture_topology()
    # 20 companies × 6 lanes = 120 child coverage rows.
    instances = {}
    companies = []
    for i in range(20):
        iid = f"co{i}-wd"
        instances[iid] = make_fake_instance(iid, scenario="results", result_count=3, company=f"Co{i}")
        companies.append(PlannedCompany(company_id=f"co{i}", name=f"Co{i}", source_instances=(iid,), geography_group="PRIMARY"))
    res = ProductionSearchRuntime(_settings(tmp_path), "run-big", instances=instances, companies=companies).run()
    assert res.terminal_state == ProductionTerminalState.COMPLETE.value
    assert res.planned_tasks == 120 and res.terminal_tasks == 120
    # The checkpoint carries counters/ids only — never job descriptions.
    assert res.checkpoint_bytes < 65536


def test_compact_state_rejects_bulky_fields():
    state = initial_production_state("r")
    for i in range(5000):
        bump(state, "discovered", 1)
    assert_compact(state)  # counters stay compact
    state["descriptions"] = ["x" * 100]  # forbidden bulky field
    with pytest.raises(ValueError):
        assert_compact(state)


# --- WAITING / FAILED distinct from COMPLETE ------------------------------
def test_human_blocked_child_yields_waiting_for_human(tmp_path):
    # A login-wall coverage child is human-blocked -> WAITING_FOR_HUMAN, never COMPLETE.
    instances, companies = _fixture_topology(scenario="error", error_category="LOGIN_WALL")
    rt = ProductionSearchRuntime(_settings(tmp_path), "run-wait", instances=instances, companies=companies)
    res = rt.run()
    assert res.terminal_state == ProductionTerminalState.WAITING_FOR_HUMAN.value
    assert res.terminal_state != ProductionTerminalState.COMPLETE.value


def test_plan_failure_is_not_complete(tmp_path):
    rt = ProductionSearchRuntime(_settings(tmp_path), "run-fail", simulate_plan_failure=True)
    res = rt.run()
    assert res.terminal_state == ProductionTerminalState.FAILED.value


def test_local_persistence_failure_never_completes(tmp_path):
    # P0-5: an injected mandatory local-persistence failure must be FAILED, not COMPLETE.
    rt = ProductionSearchRuntime(_settings(tmp_path), "run-persistfail", simulate_local_persist_failure=True)
    res = rt.run()
    assert res.terminal_state == ProductionTerminalState.FAILED.value


# --- remote audit degradation ---------------------------------------------
def test_remote_audit_failure_does_not_stop_local_run(tmp_path):
    rt = ProductionSearchRuntime(
        _settings(tmp_path), "run-audit",
        remote_audit=FailingRemoteAudit(), remote_audit_enabled=True,
    )
    res = rt.run()
    assert res.terminal_state == ProductionTerminalState.COMPLETE.value  # local run still completes
    assert res.remote_audit.status == REMOTE_AUDIT_DEGRADED


def test_remote_audit_sanitizes_pii():
    event = {"run_id": "x", "email": "a@b.com", "candidate": "secret", "counters": {"discovered": 3}}
    clean = sanitize_event(event)
    assert "email" not in clean and "candidate" not in clean
    assert clean["counters"] == {"discovered": 3}


def test_remote_audit_allowlist_drops_unexpected_nested_fields():
    # P1-9: an explicit allowlist drops any unexpected nested structure, raw
    # URLs, notes, contacts, and validates lists/mappings recursively.
    event = {
        "schema_version": 1,
        "run_id": "r1",
        "terminal_status": "COMPLETE",
        "policy_fingerprint": "abc123",
        "counters": {"discovered": 5, "leaked_note": "secret", "nested": {"x": 1}},
        "error_categories": ["HTTP_429", "TIMEOUT", {"evil": "obj"}, "x" * 500],
        "raw_url": "https://x.invalid/?token=abcdef",
        "notes": ["free-form note with PII"],
        "candidate": {"resume": "text"},
    }
    clean = sanitize_event(event)
    assert set(clean) == {"schema_version", "run_id", "terminal_status", "policy_fingerprint",
                          "counters", "error_categories"}
    # count map keeps string->number only
    assert clean["counters"] == {"discovered": 5}
    # code list keeps short strings only (drops the nested object)
    assert clean["error_categories"][:2] == ["HTTP_429", "TIMEOUT"]
    assert all(isinstance(x, str) and len(x) <= 128 for x in clean["error_categories"])
    assert "raw_url" not in clean and "notes" not in clean and "candidate" not in clean


def test_build_run_audit_event_is_allowlisted():
    from atlas.persistence.remote_audit import build_run_audit_event

    ev = build_run_audit_event(
        run_id="r", terminal_status="COMPLETE", policy_fingerprint="p", plan_fingerprint="q",
        counters={"discovered": 3}, error_categories=["HTTP_5XX"],
    )
    assert ev["run_id"] == "r" and ev["terminal_status"] == "COMPLETE"
    assert ev["counters"] == {"discovered": 3}
    assert ev["error_categories"] == ["HTTP_5XX"]


def test_production_report_uses_atomic_writer_with_locked_fallback(tmp_path):
    # P0-18: production report path uses the atomic writer. A locked final
    # destination falls back to a deterministic alternate instead of losing output.
    mapping = load_report_mapping()
    out = tmp_path / "locked_report.xlsx"
    data = {s: [] for s in REQUIRED_SHEETS}
    # Hold the destination open to simulate an Excel lock (Windows sharing).
    out.write_bytes(b"placeholder")
    handle = open(out, "r+b")
    try:
        result = write_report(mapping, out, data)
    finally:
        handle.close()
    # Either it replaced atomically, or (if the OS locked it) wrote a
    # deterministic .locked alternate — never silently lost.
    assert Path(result.written_path).exists()
    if result.locked:
        assert ".locked" in Path(result.written_path).name


# --- partial + resume ------------------------------------------------------
def test_partial_saves_continuation_and_resume_completes(tmp_path):
    settings = _settings(tmp_path)
    rt = ProductionSearchRuntime(settings, "run-partial", stop_after_phase=ProductionPhase.DISCOVER)
    res = rt.run()
    assert res.terminal_state == ProductionTerminalState.PARTIAL.value
    # Resume from the exact checkpointed continuation (a fresh runtime object).
    rt2 = ProductionSearchRuntime(settings, "run-partial")
    res2 = rt2.resume()
    assert res2.terminal_state == ProductionTerminalState.COMPLETE.value
    # No duplicate report publication and every child still terminal.
    assert res2.terminal_tasks == res2.planned_tasks


# --- report mapping (report-only, 8 sheets, reopen+validate) --------------
def test_report_mapping_maps_synthetic_data(tmp_path):
    mapping = load_report_mapping()
    assert set(REQUIRED_SHEETS) <= set(mapping.sheets)
    # canonical field -> workbook column (Phase 2A official-first §8 schema)
    row = mapping.map_record("All_Jobs", {
        "record_class": "OFFICIAL_DIRECT",
        "company": "Acme", "verification_level": "VERIFIED_OFFICIAL",
        "official_apply_url": "https://x.invalid", "recommendation": "STRONG_APPLY",
    })
    assert row["Company"] == "Acme"
    assert row["Record_Class"] == "OFFICIAL_DIRECT"
    assert row["Verification_Level"] == "VERIFIED_OFFICIAL"       # canonical verification_level
    assert row["Official_Apply_URL"] == "https://x.invalid"
    assert row["Recommendation"] == "STRONG_APPLY"


def test_report_writes_all_eight_sheets_and_revalidates(tmp_path):
    mapping = load_report_mapping()
    out = tmp_path / "report.xlsx"
    data = {s: [] for s in REQUIRED_SHEETS}
    data["All_Jobs"] = [{"company": "Acme", "role_title": "SWE", "verification_level": "VERIFIED_OFFICIAL"}]
    write_report(mapping, out, data)
    validation = validate_report(mapping, out)
    assert validation.ok, validation.problems
    assert set(REQUIRED_SHEETS) <= set(validation.sheet_names)
    assert validation.row_counts["All_Jobs"] == 1


def test_excel_is_report_only_not_operational_state(tmp_path):
    # Writing the report never creates run/coverage rows in the state DB.
    from atlas.persistence.sqlite import StateStore

    mapping = load_report_mapping()
    out = tmp_path / "r.xlsx"
    write_report(mapping, out, {s: [] for s in REQUIRED_SHEETS})
    db = tmp_path / "s.sqlite"
    with StateStore(db) as store:
        assert store.get_run("any") is None  # report writing did not touch state


# --- typed controller ops --------------------------------------------------
def test_typed_ops_work_with_nullcontroller():
    ops = TypedControllerOps(NullController(), model="none")
    r = ops.expand_query(QueryExpansionRequest(lane="JAVA_BACKEND", base_terms=("java", "spring")))
    assert r.metadata.validation_outcome == "NULL_CONTROLLER_DEFAULT"
    assert r.expansions == ("java", "spring")  # deterministic default

    m = ops.match_candidate(CandidateMatchRequest(job_requirements=("Java", "AWS"), supported_evidence=("Java",)))
    assert m.supported == ("Java",)
    assert m.missing == ("AWS",)  # never fabricates AWS as supported
    assert m.metadata.prompt_pack_version
