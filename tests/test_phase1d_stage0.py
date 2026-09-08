"""Phase 1D Stage 0 — carry-forward correctness fixes (failing-first).

Each test pins a specific Phase 1C-B correctness gap the master build must
close BEFORE portals are wired in:

  §5.1 trust enforcement — an untrusted career URL is never fetched, routed, or
       planned; persisted trust reflects the REAL TrustDecision, never a
       hard-coded True; entry identity includes company_id.
  §5.2 known-ATS fast routing — a known ATS URL routes to its structured adapter
       BEFORE (and despite) a blocked/aged HTML landing page.
  §5.3 domain-only discovery — a guessed common path is a LEAD (validated=0)
       until reachability evidence validates it; a nonexistent /careers is never
       a resolved entry.
  §5.7 site-profile lifecycle — a successful recipe is marked HEALTHY with a
       last_success/validation time.
  §5.8 new-run vs resume — a completed run_id cannot be silently reused as a
       fresh live test.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from atlas.careers.discovery import CareerEntryPoint, CareerSourceDiscoveryService
from atlas.careers.profile import RecipeHealth
from atlas.careers.router import CareerSourceRouter
from atlas.careers.trust import OfficialUrlTrustPolicy
from atlas.persistence.sqlite import StateStore

pytestmark = pytest.mark.integration


# --- §5.2 known-ATS fast routing --------------------------------------------
def test_known_ats_routes_before_blocked_landing():
    """A Greenhouse URL whose landing page is BLOCKED (403/challenge) still
    routes to the structured ATS adapter — the block never disables the API."""
    router = CareerSourceRouter()
    url = "https://boards.greenhouse.io/acme"
    decision = router.route(url, company_id="co-acme", html=None, status=403, challenge=True)
    from atlas.careers.profile import RouteKind

    assert decision.route_kind == RouteKind.ATS, decision.route_kind
    assert decision.source_instance is not None


def test_known_ats_routes_without_fetching_landing():
    """With NO page evidence at all a known ATS host still routes to ATS —
    proving the fingerprint happens before any landing-page fetch."""
    router = CareerSourceRouter()
    decision = router.route("https://acme.wd1.myworkdayjobs.com/en-US/careers",
                            company_id="co-acme", html=None)
    from atlas.careers.profile import RouteKind

    assert decision.route_kind == RouteKind.ATS


# --- §5.1 entry identity includes company_id --------------------------------
def test_entry_id_includes_company_id():
    """Two companies whose careers pages share a URL must get DISTINCT append-
    only entry observations (identity includes company_id)."""
    a = CareerEntryPoint(url="https://careers.example.com/", label="x", discovery_method="COMMON_PATH",
                         trusted=True, trust_kind="OFFICIAL_DOMAIN", confidence=0.5, company_id="co-a")
    b = CareerEntryPoint(url="https://careers.example.com/", label="x", discovery_method="COMMON_PATH",
                         trusted=True, trust_kind="OFFICIAL_DOMAIN", confidence=0.5, company_id="co-b")
    assert a.entry_id != b.entry_id


# --- §5.3 common path is a LEAD until validated -----------------------------
def test_common_path_candidate_is_lead_not_validated():
    """A discovered common path (/careers) whose host is official is TRUSTED by
    shape but NOT validated — it is a lead until reachability evidence."""
    svc = CareerSourceDiscoveryService(http_client=None)
    outcome = svc.discover("acme.com", company_id="co-acme", name="Acme", fetch=False)
    commons = [e for e in outcome.entry_points if e.discovery_method in ("COMMON_PATH", "SUBDOMAIN")]
    assert commons, "expected common-path/subdomain leads"
    # A shape-trusted common path is not, by itself, a validated entry point.
    assert all(not e.validated for e in commons)


# --- integration server for the pilot fixes ---------------------------------
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        return

    def _send(self, body, code=200, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        self.server.hits.append(path)
        host = f"127.0.0.1:{self.server.server_address[1]}"
        if path == "/careers":
            body = ("<html><head><title>Careers</title>"
                    "<script type=\"application/ld+json\">[{\"@type\":\"JobPosting\","
                    "\"title\":\"Java Backend Engineer\",\"datePosted\":\"2026-08-01\","
                    "\"url\":\"http://HOST/jobs/1\",\"identifier\":\"1\","
                    "\"jobLocation\":{\"address\":{\"addressLocality\":\"Bengaluru\"}}}]</script>"
                    "</head><body>Careers content</body></html>").replace("HOST", host)
            return self._send(body)
        if path == "/missing-careers":
            return self._send("<html><body>not found</body></html>", code=404)
        return self._send("<html><body>home</body></html>")


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.hits = []
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd
    httpd.shutdown()


def _settings(tmp_path):
    from atlas.config import load_settings

    s = load_settings(
        state_db=tmp_path / "state" / "atlas_state.sqlite",
        checkpoint_db=tmp_path / "state" / "atlas_checkpoints.sqlite",
        output_dir=tmp_path / "output", logs_dir=tmp_path / "logs",
        browser_profile=tmp_path / "profile", agents_dir=tmp_path / "agents", skills_dir=tmp_path / "skills",
    )
    s.ensure_directories()
    return s


# --- §5.1 untrusted URL never fetched / planned (pilot integration) ---------
def test_untrusted_entry_never_fetched_or_planned(tmp_path, server):
    from atlas.careers.pilot import CareerPilot, PilotCompany, PilotConfig

    base = f"http://127.0.0.1:{server.server_address[1]}"
    server.hits.clear()
    # official_domain is acme.com but the careers URL points at 127.0.0.1 — an
    # UNTRUSTED host relative to the official domain.
    config = PilotConfig(
        run_id="untrusted-1",
        companies=[PilotCompany(company_id="co-acme", name="Acme", official_domain="acme.com",
                                known_careers_url=f"{base}/careers")],
    )
    pilot = CareerPilot(_settings(tmp_path), config)
    result = pilot.run(live=True)
    # The untrusted URL was NEVER fetched.
    assert "/careers" not in server.hits, f"untrusted URL was fetched: {server.hits}"
    # It was persisted with the REAL (untrusted) decision, not trusted=True.
    with StateStore(_settings(tmp_path).state_db) as store:
        entries = store.list_career_entry_points(company_id="co-acme")
        assert entries, "entry not persisted"
        assert all(row["trusted"] == 0 for row in entries), "untrusted entry persisted as trusted"
    # It never became a planned/routable company.
    assert all(c.execution == "NONE" for c in result.companies)


# --- §5.3 nonexistent common path is not a validated resolved entry ---------
def test_domain_only_missing_careers_not_validated(tmp_path, server):
    from atlas.careers.pilot import CareerPilot, PilotCompany, PilotConfig

    base = f"http://127.0.0.1:{server.server_address[1]}"
    # official_domain = 127.0.0.1:port so the common-path host IS official, but
    # /missing-careers 404s — it must NOT be a validated entry point.
    host = f"127.0.0.1:{server.server_address[1]}"
    config = PilotConfig(
        run_id="domainonly-1",
        companies=[PilotCompany(company_id="co-missing", name="Missing", official_domain=host,
                                known_careers_url=f"{base}/missing-careers")],
    )
    pilot = CareerPilot(_settings(tmp_path), config)
    pilot.run(live=True)
    with StateStore(_settings(tmp_path).state_db) as store:
        entries = store.list_career_entry_points(company_id="co-missing")
        assert entries
        # The 404 common path is trusted-by-host but NOT validated.
        assert all(row["validated"] == 0 for row in entries), "a 404 careers path was marked validated"


# --- §5.7 successful recipe marked HEALTHY ----------------------------------
@pytest.mark.browser
def test_profile_marked_healthy_after_success(tmp_path, server):
    from atlas.careers.pilot import CareerPilot, PilotCompany, PilotConfig

    base = f"http://127.0.0.1:{server.server_address[1]}"
    host = f"127.0.0.1:{server.server_address[1]}"
    config = PilotConfig(
        run_id="healthy-1", max_pages=1,
        companies=[PilotCompany(company_id="co-ok", name="OK", official_domain=host,
                                known_careers_url=f"{base}/careers", expected_route="GENERIC_HTTP")],
    )
    pilot = CareerPilot(_settings(tmp_path), config)
    result = pilot.run(live=True)
    with StateStore(_settings(tmp_path).state_db) as store:
        profiles = store.list_career_profiles(company_id="co-ok")
        assert profiles, "no profile persisted"
        healthy = [p for p in profiles if p["health"] == RecipeHealth.HEALTHY.value]
        assert healthy, f"successful recipe not marked HEALTHY: {[p['health'] for p in profiles]}"
        assert healthy[0]["last_success_at"], "HEALTHY profile missing last_success_at"


# --- §5.8 completed run id cannot be silently reused -------------------------
def test_completed_run_id_not_silently_reused(tmp_path, server):
    from atlas.careers.pilot import CareerPilot, PilotCompany, PilotConfig, PilotRunReuseError

    base = f"http://127.0.0.1:{server.server_address[1]}"
    host = f"127.0.0.1:{server.server_address[1]}"
    cfg = lambda: PilotConfig(
        run_id="reuse-1", max_pages=1,
        companies=[PilotCompany(company_id="co-r", name="R", official_domain=host,
                                known_careers_url=f"{base}/careers")],
    )
    settings = _settings(tmp_path)
    CareerPilot(settings, cfg()).run(live=True)
    # A second FRESH run with the same completed id must not silently pretend to
    # be a new live test.
    with pytest.raises(PilotRunReuseError):
        CareerPilot(settings, cfg()).run(live=True)
    # ...but an explicit resume is allowed.
    CareerPilot(settings, cfg()).run(live=True, resume=True)
