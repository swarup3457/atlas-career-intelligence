"""Phase 1C-A FINAL stabilization — EXPLICIT human resume (build spec 4).

Failing-first regressions against the corrective defect that ordinary
``resume()`` silently reset BLOCKED_HUMAN children (inferring a human
authorization that never happened). Ordinary resume must leave a human-blocked
child blocked; only an explicit ``resume_after_human(reason, reference)`` may
reopen it — atomically (coverage + lease can't diverge), audited, and idempotent
by an operation key.
"""

from __future__ import annotations

import pytest

from atlas.config import load_settings
from atlas.orchestration.production_state import ProductionTerminalState
from atlas.persistence.sqlite import StateStore, LeaseMutationResult
from atlas.planning import PlannedCompany
from atlas.runtime.production import ProductionSearchRuntime, default_fixture_registry
from atlas.sources.coverage import CoverageStatus
from atlas.sources.models import SourceInstance, SourceType
from atlas.sources.rate_limit import ManualClock, RateLimiter, RatePolicy
from atlas.sources.testing.fake import make_fake_instance

pytestmark = pytest.mark.integration


def _fast_limiter():
    clock = ManualClock()
    return RateLimiter(RatePolicy(), clock=clock.time, sleeper=clock.sleep)


def _settings(tmp_path):
    s = load_settings(
        state_db=tmp_path / "state" / "st.sqlite", checkpoint_db=tmp_path / "state" / "cp.sqlite",
        output_dir=tmp_path / "out", logs_dir=tmp_path / "logs", browser_profile=tmp_path / "prof",
        agents_dir=tmp_path / "agents", skills_dir=tmp_path / "skills",
    )
    s.ensure_directories()
    return s


def _topology(login_scenario="error"):
    """One human-blocked company + one normal company, one lane each."""
    instances = {
        "human-inst": make_fake_instance("human-inst", scenario=login_scenario,
                                         error_category="LOGIN_WALL", result_count=2, company="Co-Human"),
        "ok-inst": make_fake_instance("ok-inst", scenario="results", result_count=2, company="Co-OK"),
    }
    companies = [
        PlannedCompany(company_id="human", name="Co-Human", source_instances=("human-inst",), geography_group="PRIMARY"),
        PlannedCompany(company_id="ok", name="Co-OK", source_instances=("ok-inst",), geography_group="PRIMARY"),
    ]
    return instances, companies


def _runtime(settings, instances, companies):
    return ProductionSearchRuntime(
        settings, "hr", instances=instances, companies=companies, registry=default_fixture_registry(),
        rate_limiter=_fast_limiter(), lane_override=["JAVA_BACKEND"],
    )


# ---------------------------------------------------------------------------
# TEST C — ordinary resume does NOT human-reopen
# ---------------------------------------------------------------------------
def test_ordinary_resume_leaves_blocked_child_blocked(tmp_path):
    settings = _settings(tmp_path)
    instances, companies = _topology(login_scenario="error")
    res = _runtime(settings, instances, companies).run()
    assert res.terminal_state == ProductionTerminalState.WAITING_FOR_HUMAN.value

    # Even with the wall now "cleared", ORDINARY resume must not reopen the
    # human-blocked child — it stays blocked and the run stays WAITING_FOR_HUMAN.
    cleared, _ = _topology(login_scenario="results")
    res2 = _runtime(settings, cleared, companies).resume()
    assert res2.terminal_state == ProductionTerminalState.WAITING_FOR_HUMAN.value
    with StateStore(settings.state_db) as store:
        blocked = [c for c in store.list_coverage("hr") if c["status"] == "BLOCKED_HUMAN"]
        assert blocked, "the human-blocked child must remain BLOCKED_HUMAN after an ordinary resume"
        # No HUMAN_REOPEN event was emitted by the ordinary resume.
        for c in blocked:
            events = [e["event"] for e in store.list_coverage_lease_events(coverage_id=c["coverage_id"])]
            assert "HUMAN_REOPEN" not in events


# ---------------------------------------------------------------------------
# TEST D — explicit human resume is atomic, audited, idempotent
# ---------------------------------------------------------------------------
def test_resume_after_human_requires_nonempty_reason(tmp_path):
    settings = _settings(tmp_path)
    instances, companies = _topology(login_scenario="error")
    rt = _runtime(settings, instances, companies)
    rt.run()
    with pytest.raises(ValueError):
        rt.resume_after_human("")
    with pytest.raises(ValueError):
        rt.resume_after_human("   ")


def test_explicit_human_resume_reopens_and_completes(tmp_path):
    settings = _settings(tmp_path)
    instances, companies = _topology(login_scenario="error")
    res = _runtime(settings, instances, companies).run()
    assert res.terminal_state == ProductionTerminalState.WAITING_FOR_HUMAN.value

    # The blocking condition is removed; an EXPLICIT authorized human resume
    # reopens ONLY the blocked child and reaches COMPLETE.
    cleared, _ = _topology(login_scenario="results")
    res2 = _runtime(settings, cleared, companies).resume_after_human(
        "login wall cleared by operator", reference="OPS-42")
    assert res2.terminal_state == ProductionTerminalState.COMPLETE.value
    assert res2.terminal_tasks == res2.planned_tasks

    with StateStore(settings.state_db) as store:
        # The previously-blocked child is now terminal and its reopen is audited once.
        human_child = [c for c in store.list_coverage("hr") if c["source_instance"] == "human-inst"][0]
        assert human_child["status"] in (
            CoverageStatus.COMPLETED_WITH_RESULTS.value, CoverageStatus.ATTEMPTED_ZERO.value)
        events = [e["event"] for e in store.list_coverage_lease_events(coverage_id=human_child["coverage_id"])]
        assert events.count("HUMAN_REOPEN") == 1


def test_human_reopen_child_is_atomic_idempotent_and_only_for_blocked(tmp_path):
    """StateStore-level guarantees: only a BLOCKED_HUMAN child reopens; coverage
    and lease change together (version++); a duplicate op_key is a harmless
    no-op; a normal terminal child is never reopened."""
    store = StateStore(tmp_path / "s.sqlite"); store.create_run("run", "none")
    # A blocked child WITH a lease (parallel path).
    store.upsert_coverage("cb", "run", "i0", status="BLOCKED_HUMAN", attempted=True, completed=False)
    store.ensure_coverage_lease("cb", "run", source_instance="i0")
    store._conn.execute("UPDATE coverage_leases SET status='BLOCKED_HUMAN', terminal=1, version=1 WHERE run_id='run' AND coverage_id='cb'")
    store._conn.commit()
    # A normally-completed child.
    store.upsert_coverage("cd", "run", "i0", status="COMPLETED_WITH_RESULTS", attempted=True, completed=True)
    store.ensure_coverage_lease("cd", "run", source_instance="i0", terminal=True)

    # A normal terminal child is never reopened.
    assert store.human_reopen_child("run", "cd", reason="oops", op_key="op-cd") == LeaseMutationResult.STALE_TOKEN_REJECTED
    assert store.get_coverage("cd", "run")["status"] == "COMPLETED_WITH_RESULTS"

    # The blocked child reopens: coverage -> NOT_ATTEMPTED AND lease -> AVAILABLE
    # with an incremented fencing version, atomically.
    assert store.human_reopen_child("run", "cb", reason="human cleared it", reference="T-1", op_key="op-cb") == LeaseMutationResult.APPLIED
    assert store.get_coverage("cb", "run")["status"] == "NOT_ATTEMPTED"
    lease = store.get_coverage_lease("cb", "run")
    assert lease["status"] == "AVAILABLE" and lease["terminal"] == 0 and lease["version"] == 2

    # Idempotent by op_key: a duplicate reopen is a harmless no-op (recorded once).
    assert store.human_reopen_child("run", "cb", reason="human cleared it", reference="T-1", op_key="op-cb") == LeaseMutationResult.ALREADY_APPLIED_IDEMPOTENTLY
    events = [e["event"] for e in store.list_coverage_lease_events(coverage_id="cb")]
    assert events.count("HUMAN_REOPEN") == 1
    assert store.get_coverage_lease("cb", "run")["version"] == 2  # not bumped again
    store.close()
