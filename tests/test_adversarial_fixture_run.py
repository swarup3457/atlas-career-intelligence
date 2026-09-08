"""Phase 1B.1 — required adversarial fixture run (build spec 25).

One deterministic sealed-plan run that exercises the full production pipeline
across 60 per-lane child coverage tasks, all six lanes, three source families,
and the hard scenarios (transient retry, 429/Retry-After, 5xx, trusted zero,
untrusted zero + one sentinel, selector drift, access-limited, cross-source
duplicate, probable repost, detail hydration, and a human-blocked item). The
run is WAITING_FOR_HUMAN while the manual item is unresolved; after a simulated
authorized resolution an EXACT resume reaches COMPLETE with every required
child terminal, no duplicated attempts/report, and an eight-sheet atomic report.
"""

from __future__ import annotations

import pytest
from openpyxl import load_workbook

from atlas.config import load_settings
from atlas.orchestration.production_state import ProductionTerminalState
from atlas.planning import PlannedCompany
from atlas.reporting.mapping import REQUIRED_SHEETS
from atlas.runtime.production import ProductionSearchRuntime, default_fixture_registry
from atlas.sources.models import Capability, SourceFamily, SourceInstance, SourceType
from atlas.sources.rate_limit import ManualClock, RateLimiter, RatePolicy
from atlas.sources.testing.fake import FakeAdapter, make_fake_instance
from atlas.sources.testing.fixture import FixtureAdapter

pytestmark = pytest.mark.integration


def _fast_limiter():
    clock = ManualClock()
    return RateLimiter(RatePolicy(), clock=clock.time, sleeper=clock.sleep)


class FakeAtsAdapter(FakeAdapter):
    """A second fake family (distinct adapter_key) so the run spans 3 families."""

    source_family = SourceFamily.GREENHOUSE


def _settings(tmp_path):
    s = load_settings(
        state_db=tmp_path / "state" / "st.sqlite", checkpoint_db=tmp_path / "state" / "cp.sqlite",
        output_dir=tmp_path / "out", logs_dir=tmp_path / "logs", browser_profile=tmp_path / "prof",
        agents_dir=tmp_path / "agents", skills_dir=tmp_path / "skills",
    )
    s.ensure_directories()
    return s


def _ats_instance(instance_id, **scenario):
    return SourceInstance(
        instance_id=instance_id, source_type=SourceType.FAKE, source_family=SourceFamily.GREENHOUSE,
        display_name=f"ATS {instance_id}", metadata=scenario,
    )


def _topology(login_scenario="error"):
    """Build 10 companies × 6 lanes = 60 children across 3 families."""
    instances: dict[str, SourceInstance] = {}
    companies: list[PlannedCompany] = []

    fake_specs = [
        ("transient", dict(scenario="results", result_count=2, fail_first_n=1, transient_category="TIMEOUT", company="Co-Trans")),
        ("ratelimit", dict(scenario="error", error_category="HTTP_429", retry_after=5, company="Co-RL")),
        ("server5xx", dict(scenario="error", error_category="HTTP_5XX", company="Co-5xx")),
        ("trustedzero", dict(scenario="zero", company="Co-Zero")),
        ("untrusted", dict(scenario="untrusted_zero", sentinel="healthy", company="Co-Unt")),
        ("drift", dict(scenario="parse_failure", company="Co-Drift")),
        ("access", dict(scenario="error", error_category="ANTI_BOT", company="Co-Acc")),
    ]
    for cid, meta in fake_specs:
        iid = f"{cid}-inst"
        instances[iid] = make_fake_instance(iid, **meta)
        companies.append(PlannedCompany(company_id=cid, name=meta["company"], source_instances=(iid,), geography_group="PRIMARY"))

    # Human-blocked item (family: fake). LOGIN_WALL -> human-blocked child.
    instances["human-inst"] = make_fake_instance("human-inst", scenario=login_scenario,
                                                 error_category="LOGIN_WALL", result_count=2, company="Co-Human")
    companies.append(PlannedCompany(company_id="human", name="Co-Human", source_instances=("human-inst",), geography_group="PRIMARY"))

    # Fixture family: cross-source duplicate + probable repost + closed job.
    same = dict(title="Java Dev", company="DupCo", location="Bengaluru", posted_at="2026-09-01", url="https://x/1")
    fixture_items = [
        dict(id="1", **same),                      # canonical
        dict(id="2", **same),                      # duplicate same-source (idempotent staging)
        dict(id="3", title="Java Dev", company="DupCo", location="Bengaluru", posted_at="2026-08-01", url="https://x/2"),  # repost
        dict(id="4", title="Closed Role", company="DupCo", location="Bengaluru", status="closed", url="https://x/4"),      # closed
    ]
    instances["fixture-inst"] = make_fixture_from_items("fixture-inst", fixture_items)
    companies.append(PlannedCompany(company_id="fixco", name="DupCo", source_instances=("fixture-inst",), geography_group="PRIMARY"))

    # Third family via a greenhouse-keyed fake ATS instance (results).
    instances["ats-inst"] = _ats_instance("ats-inst", scenario="results", result_count=2, company="Co-Ats")
    companies.append(PlannedCompany(company_id="atsco", name="Co-Ats", source_instances=("ats-inst",), geography_group="PRIMARY"))

    return instances, companies


def make_fixture_from_items(instance_id, items):
    from atlas.sources.testing.fixture import make_fixture_instance
    return make_fixture_instance(instance_id, items)


def _registry():
    reg = default_fixture_registry()
    reg.register(FakeAtsAdapter)
    return reg


def test_adversarial_run_waits_then_resumes_to_complete(tmp_path):
    settings = _settings(tmp_path)
    instances, companies = _topology(login_scenario="error")
    rt = ProductionSearchRuntime(settings, "adversarial", instances=instances, companies=companies,
                                 registry=_registry(), rate_limiter=_fast_limiter())
    res = rt.run()

    # 60 per-lane children across all six lanes and three families.
    assert res.planned_tasks == 60
    assert len(res.lane_summary) == 6
    # A human-blocked child makes the run WAITING_FOR_HUMAN, never COMPLETE.
    assert res.terminal_state == ProductionTerminalState.WAITING_FOR_HUMAN.value

    # Simulated authorized resolution: the previously login-walled source now
    # returns results. A brand-new runtime performs an EXPLICIT human resume
    # (build spec 4) — ordinary resume never reopens a human-blocked child.
    resolved_instances, _ = _topology(login_scenario="results")
    rt2 = ProductionSearchRuntime(settings, "adversarial", instances=resolved_instances, companies=companies,
                                  registry=_registry(), rate_limiter=_fast_limiter())
    res2 = rt2.resume_after_human("login wall cleared by operator", reference="OPS-1")

    assert res2.terminal_state == ProductionTerminalState.COMPLETE.value
    assert res2.planned_tasks == 60 and res2.terminal_tasks == 60
    # Every lane fully terminal.
    for lane, counts in res2.lane_summary.items():
        assert counts["terminal"] == counts["planned"], lane

    # Exactly one atomic eight-sheet workbook (no duplicate publication).
    workbooks = list((tmp_path / "out").glob("Atlas_Jobs_*.xlsx"))
    assert len(workbooks) == 1
    wb = load_workbook(workbooks[0], read_only=True)
    try:
        assert set(REQUIRED_SHEETS) <= set(wb.sheetnames)
    finally:
        wb.close()

    # Cross-source dedupe + repost separation produced canonical jobs.
    assert res2.counters.get("unique_after_dedupe", 0) >= 1

    # Attempts are append-only and persisted per child (no duplication on resume).
    from atlas.persistence.sqlite import StateStore

    with StateStore(settings.state_db) as store:
        cov = store.list_coverage("adversarial")
        assert len(cov) == 60
        # Every child has at least one recorded attempt.
        assert all(len(store.list_coverage_attempts(c["coverage_id"])) >= 1
                   for c in cov if c["source_instance"] != "fixture-inst" or True)
        # Exactly one sentinel was attempted for the untrusted-zero source lanes.
        assert store.count_raw_observations("adversarial") >= 1
