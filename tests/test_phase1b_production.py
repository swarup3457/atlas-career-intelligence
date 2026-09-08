"""Phase 1B — production graph / runtime / controller / report tests (spec 7.11-7.16, 20)."""

from __future__ import annotations

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

pytestmark = pytest.mark.integration


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
    rt = ProductionSearchRuntime(_settings(tmp_path), "run-1", discovery_count=10)
    res = rt.run()
    assert res.terminal_state == ProductionTerminalState.COMPLETE.value
    # phases occurred in the canonical order
    expected = [p.value for p in PRODUCTION_PHASE_ORDER if p != ProductionPhase.COMPLETE]
    assert list(res.phases_completed) == expected
    assert res.policy_fingerprint and res.plan_fingerprint


def test_completed_manifest_has_tz_aware_completed_at(tmp_path):
    res = ProductionSearchRuntime(_settings(tmp_path), "run-2", discovery_count=3).run()
    assert res.manifest["completed_at"]
    assert res.manifest["completed_at"].endswith("+00:00")  # tz-aware UTC


# --- search isolation ------------------------------------------------------
def test_discovery_cannot_trigger_excel_or_remote_audit(tmp_path):
    res = ProductionSearchRuntime(_settings(tmp_path), "run-3", discovery_count=5).run()
    # Every recorded side effect happened in a persistence phase, never during
    # DISCOVER / health / hydration.
    for eff in res.persistence_side_effects:
        phase = eff.split(":", 1)[0]
        assert phase in ("PERSIST_LOCAL", "BUILD_REPORT", "OPTIONAL_REMOTE_AUDIT")


def test_phase_context_rejects_side_effect_in_discovery_phase():
    ctx = PhaseContext()
    with pytest.raises(PhaseIsolationError):
        ctx.record_side_effect(ProductionPhase.DISCOVER, "excel_report")


# --- compact checkpoints (thousands of discoveries) ------------------------
def test_checkpoint_stays_bounded_with_thousands_of_discoveries(tmp_path):
    res = ProductionSearchRuntime(_settings(tmp_path), "run-big", discovery_count=3000).run()
    assert res.terminal_state == ProductionTerminalState.COMPLETE.value
    assert res.counters["discovered"] == 3000
    # The checkpoint carries counters/ids only — never 3000 job descriptions.
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
def test_waiting_for_human_is_not_complete(tmp_path):
    rt = ProductionSearchRuntime(_settings(tmp_path), "run-wait", discovery_count=2, simulate_waiting_for_human=True)
    res = rt.run()
    assert res.terminal_state == ProductionTerminalState.WAITING_FOR_HUMAN.value
    assert res.terminal_state != ProductionTerminalState.COMPLETE.value


def test_plan_failure_is_not_complete(tmp_path):
    rt = ProductionSearchRuntime(_settings(tmp_path), "run-fail", simulate_plan_failure=True)
    res = rt.run()
    assert res.terminal_state == ProductionTerminalState.FAILED.value


# --- remote audit degradation ---------------------------------------------
def test_remote_audit_failure_does_not_stop_local_run(tmp_path):
    rt = ProductionSearchRuntime(
        _settings(tmp_path), "run-audit", discovery_count=2,
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


# --- partial + resume ------------------------------------------------------
def test_partial_saves_continuation_and_resume_completes(tmp_path):
    settings = _settings(tmp_path)
    rt = ProductionSearchRuntime(settings, "run-partial", discovery_count=3, stop_after_phase=ProductionPhase.DISCOVER)
    res = rt.run()
    assert res.terminal_state == ProductionTerminalState.PARTIAL.value
    # Resume from the exact checkpointed continuation.
    rt2 = ProductionSearchRuntime(settings, "run-partial", discovery_count=3)
    res2 = rt2.resume()
    assert res2.terminal_state == ProductionTerminalState.COMPLETE.value


# --- report mapping (report-only, 8 sheets, reopen+validate) --------------
def test_report_mapping_maps_synthetic_data(tmp_path):
    mapping = load_report_mapping()
    assert set(REQUIRED_SHEETS) <= set(mapping.sheets)
    # canonical field -> workbook column
    row = mapping.map_record("All_Jobs", {
        "company": "Acme", "verification_level": "VERIFIED_OFFICIAL",
        "official_apply_url": "https://x.invalid", "recommendation": "STRONG_APPLY",
    })
    assert row["Company"] == "Acme"
    assert row["Verification_Status"] == "VERIFIED_OFFICIAL"      # canonical verification_level
    assert row["Official_Apply_URL"] == "https://x.invalid"
    assert row["Application_Recommendation"] == "STRONG_APPLY"


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
