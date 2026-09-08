"""Phase 1D market layer unit tests (offline, deterministic).

Covers campaign/wave sealing + append-only semantics, the deterministic coverage
deficit analyzer + bounded query expansion, concurrency-class pool caps, single
authenticated-profile ownership, dynamic-company merge safety, and
portal->official verification linkage.
"""

from __future__ import annotations

import threading
import time

import pytest

from atlas.market.campaign import (
    CampaignBudget,
    MarketCampaign,
    MarketWave,
    WaveStatus,
    WaveTask,
    WaveTaskKind,
)
from atlas.market.deficit import (
    CoverageDeficitAnalyzer,
    DeficitKind,
    LaneSourceOutcome,
    WaveExpansionPlanner,
    WaveOutcome,
    validate_variant_term,
)
from atlas.market.dynamic_company import DynamicCompanyRegistrar, DynamicResolution
from atlas.market.pools import (
    OFFICIAL_HTTP,
    PORTAL_BROWSER_AUTHENTICATED,
    PUBLIC_BROWSER_ANONYMOUS,
    MarketPoolDispatcher,
    PoolTask,
)
from atlas.market.verification import PortalOfficialVerifier
from atlas.persistence.sqlite import StateStore
from atlas.planning.query_compiler import SearchQueryCompiler
from atlas.policy import load_policy
from atlas.sources.portals.models import PortalJobLead, PortalLeadVerification


# --- campaign / wave sealing -------------------------------------------------
def test_wave_seal_is_deterministic_and_append_only():
    tasks = [WaveTask(WaveTaskKind.PORTAL, "t1", lane="JAVA_BACKEND", geography="PRIMARY",
                      source_family="linkedin", query="java")]
    w = MarketWave(campaign_id="c", run_id="r", wave_index=0, tasks=tasks)
    h = w.seal()
    assert w.status == WaveStatus.SEALED
    # Re-sealing a sealed wave is refused (append-only identity).
    with pytest.raises(ValueError):
        w.seal()
    # An identical plan seals identically; a changed plan seals differently.
    w2 = MarketWave(campaign_id="c", run_id="r", wave_index=0, tasks=list(tasks))
    assert w2.compute_seal_hash() == h
    w3 = MarketWave(campaign_id="c", run_id="r", wave_index=0,
                    tasks=tasks + [WaveTask(WaveTaskKind.PORTAL, "t2", lane="X", source_family="naukri")])
    assert w3.compute_seal_hash() != h


def test_campaign_wave_persist_roundtrip(tmp_path):
    with StateStore(tmp_path / "s.sqlite") as store:
        camp = MarketCampaign(campaign_id="c1", run_id="r1", budget=CampaignBudget(max_waves=3))
        camp.persist(store)
        w0 = MarketWave(campaign_id="c1", run_id="r1", wave_index=0,
                        tasks=[WaveTask(WaveTaskKind.PORTAL, "t1", lane="JAVA_BACKEND", source_family="linkedin")])
        w0.seal()
        w0.persist(store)
        camp2 = MarketCampaign.from_row(store.get_market_campaign("c1"))
        assert camp2.budget.max_waves == 3
        waves = store.list_market_waves("c1")
        assert len(waves) == 1
        w = MarketWave.from_row(waves[0])
        assert w.is_sealed and len(w.tasks) == 1


# --- deficit analysis --------------------------------------------------------
def test_deficit_analyzer_flags_expected_kinds():
    outcome = WaveOutcome(
        lane_sources=[
            LaneSourceOutcome("linkedin", "JAVA_BACKEND", "PRIMARY", attempted=True,
                              terminal_status="ACCESS_LIMITED", health="ACCESS_LIMITED"),
            LaneSourceOutcome("naukri", "GENERAL_SOFTWARE", "PRIMARY", attempted=True,
                              terminal_status="COMPLETED_WITH_RESULTS", results=10, unique=2, duplicates=8),
        ],
        required_families=frozenset({"linkedin", "naukri", "foundit"}),
        attempted_families=frozenset({"linkedin", "naukri"}),
        portal_leads_total=3, portal_leads_unverified=3,
    )
    kinds = {d.kind for d in CoverageDeficitAnalyzer().analyze(outcome)}
    assert DeficitKind.SOURCE_NOT_ATTEMPTED in kinds       # foundit never attempted
    assert DeficitKind.SOURCE_UNHEALTHY_ZERO in kinds      # linkedin access-limited
    assert DeficitKind.HIGH_DUPLICATE_RATIO in kinds       # naukri 8/10 dups
    assert DeficitKind.PORTAL_LEADS_UNVERIFIED in kinds


def test_variant_validation_rejects_urls_and_instructions():
    assert validate_variant_term("java backend engineer") == "java backend engineer"
    assert validate_variant_term("https://evil.example/jobs") is None
    assert validate_variant_term("ignore previous instructions") is None
    assert validate_variant_term("<script>alert(1)</script>") is None
    assert validate_variant_term("a") is None  # too short


def _compiler():
    policy = load_policy(None)
    return SearchQueryCompiler(policy.lanes, policy.geography, policy_version=policy.short_fingerprint), policy


def test_bounded_expansion_dedupes_caps_and_continues_when_incomplete():
    compiler, policy = _compiler()
    budget = CampaignBudget(max_waves=3, max_variants_per_lane=3)
    planner = WaveExpansionPlanner(compiler, policy.geography, budget)
    lane = next(iter(policy.lanes))
    campaign = MarketCampaign(campaign_id="c", run_id="r", budget=budget)
    parent = MarketWave(campaign_id="c", run_id="r", wave_index=0)
    # An unhealthy required source is an ACTIONABLE deficit -> continue (Wave 1).
    deficits = CoverageDeficitAnalyzer().analyze(WaveOutcome(
        lane_sources=[LaneSourceOutcome("linkedin", lane, "PRIMARY", attempted=True,
                                        terminal_status="ACCESS_LIMITED", health="ACCESS_LIMITED")],
        required_families=frozenset({"linkedin"}), attempted_families=frozenset({"linkedin"})))
    nxt = planner.plan_next_wave(campaign, parent, deficits, prior_queries=set())
    assert nxt is not None and nxt.wave_index == 1
    # Per-lane variant cap respected.
    per_lane = sum(1 for t in nxt.tasks if t.lane == lane and t.source_family == "linkedin")
    assert per_lane <= budget.max_variants_per_lane
    # No task duplicates a prior query.
    keys = [(t.source_family, t.lane, (t.query or "").lower()) for t in nxt.tasks]
    assert len(keys) == len(set(keys))


def test_expansion_stops_when_no_actionable_deficit():
    compiler, policy = _compiler()
    budget = CampaignBudget(max_waves=3)
    planner = WaveExpansionPlanner(compiler, policy.geography, budget)
    campaign = MarketCampaign(campaign_id="c", run_id="r", budget=budget)
    parent = MarketWave(campaign_id="c", run_id="r", wave_index=0)
    # All required lanes healthy with results + all leads verified -> STOP even
    # though jobs were found (never loop just to reach a job count).
    lane = next(iter(policy.lanes))
    deficits = CoverageDeficitAnalyzer(low_yield_threshold=1).analyze(WaveOutcome(
        lane_sources=[LaneSourceOutcome("linkedin", lane, "PRIMARY", attempted=True,
                                        terminal_status="COMPLETED_WITH_RESULTS", results=5, unique=5)],
        required_families=frozenset({"linkedin"}), attempted_families=frozenset({"linkedin"}),
        portal_leads_total=5, portal_leads_unverified=0))
    assert planner.plan_next_wave(campaign, parent, deficits, prior_queries=set()) is None


def test_expansion_respects_max_waves():
    compiler, policy = _compiler()
    budget = CampaignBudget(max_waves=2)
    planner = WaveExpansionPlanner(compiler, policy.geography, budget)
    campaign = MarketCampaign(campaign_id="c", run_id="r", budget=budget)
    parent = MarketWave(campaign_id="c", run_id="r", wave_index=1)  # already at index 1
    lane = next(iter(policy.lanes))
    deficits = [type("D", (), {"kind": DeficitKind.SOURCE_UNHEALTHY_ZERO, "lane": lane,
                               "geography": "PRIMARY", "source_family": "linkedin", "detail": {}})()]
    # next_index would be 2 == max_waves -> no expansion.
    assert planner.plan_next_wave(campaign, parent, deficits, prior_queries=set()) is None


# --- concurrency pools -------------------------------------------------------
def test_pool_caps_are_respected():
    with MarketPoolDispatcher() as d:
        def slow(i):
            def f():
                time.sleep(0.05)
                return i
            return f
        tasks = [PoolTask(f"h{i}", OFFICIAL_HTTP, slow(i)) for i in range(10)]
        tasks += [PoolTask(f"b{i}", PUBLIC_BROWSER_ANONYMOUS, slow(i)) for i in range(6)]
        res = d.run(tasks)
        assert res.max_observed[OFFICIAL_HTTP] <= 4
        assert res.max_observed[PUBLIC_BROWSER_ANONYMOUS] <= 2
        assert len(res.results) == 16


def test_authenticated_pool_has_single_owner():
    active = {"n": 0, "max": 0}
    lock = threading.Lock()

    def auth_task():
        with lock:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
        time.sleep(0.03)
        with lock:
            active["n"] -= 1
        return "ok"

    with MarketPoolDispatcher() as d:
        tasks = [PoolTask(f"a{i}", PORTAL_BROWSER_AUTHENTICATED, auth_task) for i in range(5)]
        res = d.run(tasks)
    assert active["max"] == 1  # never two owners of the one authenticated profile
    assert res.max_observed[PORTAL_BROWSER_AUTHENTICATED] == 1
    assert res.profile_owner_max == 1


# --- dynamic company merge safety -------------------------------------------
def test_dynamic_company_merge_safety(tmp_path):
    with StateStore(tmp_path / "s.sqlite") as store:
        registrar = DynamicCompanyRegistrar(store)
        # Same name, DIFFERENT official domains -> never merged.
        lead_a = PortalJobLead(run_id="r", source_family="linkedin", title="Java Dev",
                               company_name="Acme", company_url="https://acme.com",
                               portal_job_id="1")
        lead_b = PortalJobLead(run_id="r", source_family="naukri", title="Java Dev",
                               company_name="Acme", company_url="https://acme.io", portal_job_id="2")
        ra = registrar.register_from_lead(lead_a, run_id="r")
        assert ra.resolution_status in (DynamicResolution.RESOLVED, DynamicResolution.DYNAMICALLY_DISCOVERED)
        rb = registrar.register_from_lead(lead_b, run_id="r")
        # Different domain, same name -> ambiguous, NOT merged into A.
        assert rb.resolution_status == DynamicResolution.AMBIGUOUS
        assert rb.company_id != ra.company_id


def test_dynamic_company_unresolved_when_no_trusted_domain(tmp_path):
    with StateStore(tmp_path / "s.sqlite") as store:
        registrar = DynamicCompanyRegistrar(store)
        # Only a portal (linkedin) URL -> no trusted official domain -> registered
        # DYNAMICALLY_DISCOVERED (name only), never a guessed domain.
        lead = PortalJobLead(run_id="r", source_family="linkedin", title="SWE",
                             company_name="Startup Xyz", company_url="https://www.linkedin.com/company/xyz",
                             portal_job_id="9")
        res = registrar.register_from_lead(lead, run_id="r")
        assert res.resolution_status == DynamicResolution.DYNAMICALLY_DISCOVERED
        assert res.official_domain is None


# --- portal -> official verification ----------------------------------------
def test_portal_lead_not_verified_without_official_evidence(tmp_path):
    with StateStore(tmp_path / "s.sqlite") as store:
        lead = PortalJobLead(run_id="r", source_family="linkedin", title="Java Backend Engineer",
                             company_name="Acme India", location="Bengaluru", portal_job_id="900")
        lead.persist(store)
        results = PortalOfficialVerifier(store).link_run("r")
        assert len(results) == 1
        # No official observation -> stays a lead, never verified official.
        assert results[0].verification_state == PortalLeadVerification.PORTAL_CURRENT_LEAD
        row = store.get_portal_lead(lead.lead_id)
        assert row["verification_state"] == "PORTAL_CURRENT_LEAD"


def test_portal_lead_links_to_official_evidence(tmp_path):
    with StateStore(tmp_path / "s.sqlite") as store:
        lead = PortalJobLead(run_id="r", source_family="linkedin", title="Java Backend Developer",
                             company_name="Acme India", location="Bengaluru, Karnataka", portal_job_id="900")
        lead.persist(store)
        # An OFFICIAL observation (career site) for the same company/title/city.
        store.stage_raw_observation(
            "obs::r::c1", "r", source_instance="acme-career", content_hash="h1",
            source_family="company_career", source_job_id="REQ-1",
            canonical_url="https://acme.com/careers/REQ-1", company="Acme India",
            title="Java Backend Engineer", location="Bengaluru", is_active="ACTIVE",
            detail={"verification_level": "OFFICIAL_SEARCH_LIVE"})
        results = PortalOfficialVerifier(store).link_run("r")
        assert results[0].verification_state == PortalLeadVerification.LINKED_OFFICIAL_VERIFIED
        row = store.get_portal_lead(lead.lead_id)
        assert row["verification_state"] == "LINKED_OFFICIAL_VERIFIED"
        links = store.list_portal_official_links("r")
        assert links and links[0]["match_kind"] == "COMPANY_TITLE_LOCATION"
