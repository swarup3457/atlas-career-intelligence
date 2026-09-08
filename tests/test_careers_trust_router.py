"""Phase 1C-B Layer A unit tests: trust policy, router, discovery, profile, planner.

Fully offline, deterministic.
"""

from __future__ import annotations

import datetime

import pytest

from atlas.careers.discovery import CareerSourceDiscoveryService
from atlas.careers.planner import OfficialCareerCoveragePlanner, OfficialCareerTarget
from atlas.careers.profile import CareerSiteProfile, ExtractionRecipe, RecipeHealth, RouteKind
from atlas.careers.router import CareerSourceRouter
from atlas.careers.trust import OfficialUrlTrustPolicy, TrustKind
from atlas.sources.models import SourceFamily, SourceType

pytestmark = pytest.mark.unit


# --- OfficialUrlTrustPolicy -------------------------------------------------
def test_trust_exact_official_domain_and_subdomain():
    policy = OfficialUrlTrustPolicy("acme.com")
    assert policy.classify("https://acme.com/careers").kind == TrustKind.OFFICIAL_DOMAIN
    assert policy.classify("https://careers.acme.com/").kind == TrustKind.OFFICIAL_SUBDOMAIN
    assert policy.classify("https://www.acme.com/jobs").trusted


@pytest.mark.parametrize("bad", [
    "https://evilacme.com/careers",
    "https://acme.com.evil.example/careers",
    "https://acme-com.evil.example/careers",
    "https://notacme.com/careers",
])
def test_trust_rejects_lookalikes(bad):
    policy = OfficialUrlTrustPolicy("acme.com")
    d = policy.classify(bad)
    assert not d.trusted
    assert d.kind == TrustKind.UNTRUSTED


def test_trust_known_ats_host():
    policy = OfficialUrlTrustPolicy("acme.com")
    d = policy.classify("https://boards.greenhouse.io/acme")
    assert d.trusted and d.kind == TrustKind.KNOWN_ATS
    assert d.ats_source_type == SourceType.ATS_GREENHOUSE


def test_trust_posting_text_url_never_trusted():
    policy = OfficialUrlTrustPolicy("acme.com")
    d = policy.classify("https://acme.com/careers/jobs/1", from_posting_text=True)
    assert not d.trusted and d.kind == TrustKind.UNTRUSTED


def test_trust_fails_closed_without_official_domain():
    policy = OfficialUrlTrustPolicy(None)
    d = policy.classify("https://something.com/careers")
    assert not d.trusted and d.kind == TrustKind.AMBIGUOUS


def test_trust_redirect_chain_all_hops_official():
    policy = OfficialUrlTrustPolicy("acme.com")
    ok = policy.classify_redirect_chain([
        "https://acme.com/careers", "https://careers.acme.com/", "https://careers.acme.com/jobs",
    ])
    assert ok.trusted
    assert ok.redirect_chain[-1] == "https://careers.acme.com/jobs"


def test_trust_redirect_chain_offsite_hop_rejected():
    policy = OfficialUrlTrustPolicy("acme.com")
    d = policy.classify_redirect_chain(["https://acme.com/careers", "https://evil.com/x"])
    assert not d.trusted


def test_trust_redirect_downgrade_rejected():
    policy = OfficialUrlTrustPolicy("acme.com")
    d = policy.classify_redirect_chain(["https://acme.com/careers", "http://acme.com/careers"])
    assert not d.trusted
    assert "downgrade" in d.reason.lower()


def test_trust_redirect_to_known_ats_ok():
    policy = OfficialUrlTrustPolicy("acme.com")
    d = policy.classify_redirect_chain(["https://acme.com/careers", "https://boards.greenhouse.io/acme"])
    assert d.trusted and d.kind == TrustKind.KNOWN_ATS


# --- CareerSourceRouter -----------------------------------------------------
def test_router_ats_by_host():
    d = CareerSourceRouter().route("https://jobs.lever.co/acme", company_id="acme", html="<html></html>")
    assert d.route_kind == RouteKind.ATS
    assert d.fingerprint_family == SourceFamily.LEVER
    assert d.source_instance.source_type == SourceType.ATS_LEVER


def test_router_ats_embedded_marker():
    html = '<html><body><script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script></body></html>'
    d = CareerSourceRouter().route("https://acme.com/careers", company_id="acme", html=html)
    assert d.route_kind == RouteKind.ATS
    assert d.fingerprint_family == SourceFamily.GREENHOUSE


def test_router_generic_http_jsonld():
    html = "real content " * 50 + '<script type="application/ld+json">{"@type":"JobPosting","title":"E","url":"https://acme.com/j/1"}</script>'
    d = CareerSourceRouter().route("https://acme.com/careers", company_id="acme", html=html)
    assert d.route_kind == RouteKind.GENERIC_HTTP
    assert d.source_instance.source_family == SourceFamily.COMPANY_CAREER
    assert d.source_instance.metadata["recipe"]["extraction_method"] == "jsonld"


def test_router_generic_browser_spa_shell():
    d = CareerSourceRouter().route(
        "https://acme.com/careers", company_id="acme",
        html='<html><body><div id="__next"></div><script src="/x.js"></script></body></html>',
    )
    assert d.route_kind == RouteKind.GENERIC_BROWSER
    assert d.source_instance.source_family == SourceFamily.COMPANY_CAREER_BROWSER


def test_router_login_and_challenge_terminal():
    r = CareerSourceRouter()
    assert r.route("https://acme.com/careers", login_wall=True).route_kind == RouteKind.AUTH_REQUIRED
    assert r.route("https://acme.com/careers", challenge=True).route_kind == RouteKind.ACCESS_LIMITED
    assert r.route("https://acme.com/careers", status=403).route_kind == RouteKind.ACCESS_LIMITED
    assert not r.route("https://acme.com/careers", login_wall=True).routable


def test_router_persist_records_instance_and_classification(tmp_path):
    from atlas.persistence.sqlite import StateStore

    r = CareerSourceRouter()
    d = r.route("https://acme.com/careers", company_id="acme",
                html="content " * 50 + '<script type="application/ld+json">{"@type":"JobPosting","title":"E","url":"https://acme.com/j/1"}</script>')
    with StateStore(tmp_path / "s.sqlite") as store:
        r.persist(store, d, company_id="acme")
        assert store.get_source_instance(d.source_instance.instance_id) is not None
        routes = store.list_route_classifications(source_instance_id=d.source_instance.instance_id)
        assert len(routes) == 1 and routes[0]["route_kind"] == "GENERIC_HTTP"


# --- CareerSourceDiscoveryService (offline) --------------------------------
def test_discovery_offline_multi_source():
    svc = CareerSourceDiscoveryService()
    home = '<a href="/careers">Careers</a><a href="https://jobs.acme.com/">Jobs</a><a href="/about">About</a>'
    robots = "Sitemap: https://acme.com/sitemap.xml"
    sitemaps = {"https://acme.com/sitemap.xml": "<urlset><url><loc>https://acme.com/careers/jobs/1</loc></url></urlset>"}
    out = svc.discover("acme.com", company_id="acme", known_careers_url="https://acme.com/careers",
                       homepage_html=home, robots_txt=robots, sitemap_map=sitemaps, fetch=False)
    assert out.status == "RESOLVED"
    methods = {e.discovery_method for e in out.trusted_entry_points}
    assert {"USER_SUPPLIED", "NAV_LINK", "ROBOTS_SITEMAP"} <= methods
    # A company can have multiple current entry points.
    assert len(out.trusted_entry_points) >= 3


def test_discovery_rejects_untrusted_nav_offsite():
    svc = CareerSourceDiscoveryService()
    home = '<a href="https://evil.com/careers">Careers</a>'
    out = svc.discover("acme.com", company_id="acme", homepage_html=home, fetch=False)
    assert all("evil.com" not in e.url for e in out.trusted_entry_points)


def test_discovery_persist_append_only(tmp_path):
    from atlas.persistence.sqlite import StateStore

    svc = CareerSourceDiscoveryService()
    out = svc.discover("acme.com", company_id="acme", known_careers_url="https://acme.com/careers", fetch=False)
    with StateStore(tmp_path / "s.sqlite") as store:
        svc.persist(store, out)
        rows = store.list_career_entry_points(company_id="acme")
        assert len(rows) >= 1
        # Idempotent re-persist keeps the same rows (no duplicate identity).
        svc.persist(store, out)
        assert len(store.list_career_entry_points(company_id="acme")) == len(rows)


# --- CareerSiteProfile ------------------------------------------------------
def test_profile_roundtrip_and_revalidation(tmp_path):
    from atlas.persistence.sqlite import StateStore
    from atlas.careers.profile import load_profile, save_profile

    prof = CareerSiteProfile(
        profile_id="p1", company_id="acme", source_instance_id="i1",
        entry_url="https://acme.com/careers", route_kind=RouteKind.GENERIC_HTTP,
        recipe=ExtractionRecipe(extraction_method="jsonld"), confidence=0.85,
        parser_version="career-extract-1.0.0",
    )
    # Unvalidated -> always needs revalidation.
    assert prof.needs_revalidation()
    prof.mark_success(at="2026-09-01T00:00:00+00:00", confidence=0.9)
    assert prof.health == RecipeHealth.HEALTHY
    # Fresh success within ttl -> no revalidation.
    now = datetime.datetime(2026, 9, 2, tzinfo=datetime.timezone.utc)
    assert not prof.needs_revalidation(now=now)
    # Stale beyond ttl -> revalidate.
    later = datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc)
    assert prof.needs_revalidation(now=later)

    with StateStore(tmp_path / "s.sqlite") as store:
        save_profile(store, prof)
        loaded = load_profile(store, "i1")
        assert loaded is not None
        assert loaded.route_kind == RouteKind.GENERIC_HTTP
        assert loaded.recipe.extraction_method == "jsonld"
        assert loaded.health == RecipeHealth.HEALTHY


# --- OfficialCareerCoveragePlanner -----------------------------------------
def test_planner_sealed_per_lane_children():
    r = CareerSourceRouter()
    d_http = r.route("https://acme.com/careers", company_id="acme",
                     html="content " * 50 + '<script type="application/ld+json">{"@type":"JobPosting","title":"E","url":"https://acme.com/j/1"}</script>')
    d_ats = r.route("https://boards.greenhouse.io/beta", company_id="beta", html="<html></html>")
    targets = [
        OfficialCareerTarget("acme", "Acme", "acme.com", d_http),
        OfficialCareerTarget("beta", "Beta", "beta.com", d_ats),
    ]
    planner = OfficialCareerCoveragePlanner()
    plan = planner.build("run-1", targets, lanes=["JAVA_BACKEND", "GENERAL_SOFTWARE"])
    assert plan.manifest.is_sealed
    # One child per company x instance x lane => 2 companies x 2 lanes = 4.
    assert len(plan.manifest.tasks()) == 4
    assert len(plan.instances) == 2
    lanes = {t.lane for t in plan.manifest.tasks()}
    assert lanes == {"JAVA_BACKEND", "GENERAL_SOFTWARE"}


def test_planner_unroutable_gets_source_discovery_child():
    r = CareerSourceRouter()
    d = r.route("https://acme.com/careers", login_wall=True)  # AUTH_REQUIRED, not routable
    targets = [OfficialCareerTarget("acme", "Acme", "acme.com", d)]
    plan = OfficialCareerCoveragePlanner().build("run-2", targets, lanes=["GENERAL_SOFTWARE"])
    assert len(plan.unroutable) == 1
    # A source-discovery child is planned so the company is not silently dropped.
    assert any(t.source_type == "COMPANY_SOURCE_DISCOVERY" for t in plan.manifest.tasks())
