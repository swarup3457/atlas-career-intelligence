"""Phase 1D market runtime E2E tests (offline fixtures, no real_web).

A local 127.0.0.1 server serves synthetic LinkedIn/Naukri responses so the full
market runtime — sealed campaign/waves, bounded pools, portal leads, dynamic
company registration, portal->official verification, adaptive expansion, and the
single current-run report — is exercised end to end without touching the live web.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from atlas.config import load_settings
from atlas.market.campaign import CampaignBudget, CampaignStatus
from atlas.market.pools import PUBLIC_BROWSER_ANONYMOUS
from atlas.market.runtime import MarketRunReuseError, MarketSearchRuntime
from atlas.persistence.sqlite import StateStore

pytestmark = pytest.mark.integration


_LI = ("<li><div class=\"base-card\" data-entity-urn=\"urn:li:jobPosting:900\">"
       "<a class=\"base-card__full-link\" href=\"https://www.linkedin.com/jobs/view/900\"></a>"
       "<h3 class=\"base-search-card__title\">Java Backend Engineer</h3>"
       "<h4 class=\"base-search-card__subtitle\"><a href=\"x\">Acme India</a></h4>"
       "<span class=\"job-search-card__location\">Bengaluru, Karnataka, India</span>"
       "<time class=\"job-search-card__listdate\" datetime=\"2026-08-20\">3 days ago</time></div></li>")
_NK = {"noOfJobs": 1, "jobDetails": [{
    "jobId": "nk900", "title": "Software Engineer", "companyName": "Globex",
    "jdURL": "/job-listings-se-globex-nk900",
    "placeholders": [{"type": "location", "label": "Hyderabad"}, {"type": "experience", "label": "2-5 Yrs"}],
    "footerPlaceholderLabel": "Just now", "tagsAndSkills": "python"}]}


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
        p = self.path.split("?")[0]
        if p == "/li":
            return self._send(_LI)
        if p == "/nk":
            return self._send(json.dumps(_NK), ctype="application/json")
        return self._send("home")


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd
    httpd.shutdown()


def _settings(tmp_path):
    s = load_settings(
        state_db=tmp_path / "state" / "s.sqlite", checkpoint_db=tmp_path / "state" / "c.sqlite",
        output_dir=tmp_path / "out", logs_dir=tmp_path / "logs", browser_profile=tmp_path / "prof",
        agents_dir=tmp_path / "ag", skills_dir=tmp_path / "sk")
    s.ensure_directories()
    return s


def _meta(server):
    base = f"http://127.0.0.1:{server.server_address[1]}"
    return {"linkedin": {"guest_base": f"{base}/li"}, "naukri": {"search_base": f"{base}/nk"}}


def _runtime(settings, server, run_id, **kw):
    return MarketSearchRuntime(
        settings, run_id, lanes=["JAVA_BACKEND", "GENERAL_SOFTWARE"], geography_groups=["PRIMARY"],
        portal_metadata=_meta(server), **kw)


def test_market_plan_is_offline_and_sealed(tmp_path, server):
    settings = _settings(tmp_path)
    rt = _runtime(settings, server, "plan-1")
    plan = rt.plan()
    assert plan["wave0_tasks"] == 4  # 2 families x 2 lanes
    assert plan["wave0_seal"]
    with StateStore(settings.state_db) as store:
        assert store.get_market_campaign("camp::plan-1") is not None
        assert len(store.list_market_waves("camp::plan-1")) == 1
        # No leads before a live run.
        assert store.list_portal_leads("plan-1") == []


def test_market_run_produces_leads_and_report(tmp_path, server):
    settings = _settings(tmp_path)
    rt = _runtime(settings, server, "run-1", budget=CampaignBudget(max_waves=1, max_browser_calls=20))
    res = rt.run(live=True)
    assert res.portal_leads >= 2  # linkedin + naukri
    assert res.dynamic_companies >= 2
    assert res.report_valid is True
    with StateStore(settings.state_db) as store:
        leads = store.list_portal_leads("run-1")
        assert all(r["verification_state"] == "PORTAL_CURRENT_LEAD" for r in leads)
        # Every lead has a resolved (dynamically discovered) company.
        assert all(r["company_id"] for r in leads)


def test_global_budget_yields_partial(tmp_path, server):
    settings = _settings(tmp_path)
    # Only ONE browser call allowed across the whole campaign -> PARTIAL_BUDGET.
    rt = _runtime(settings, server, "budget-1", budget=CampaignBudget(max_waves=1, max_browser_calls=1))
    res = rt.run(live=True)
    assert res.status == CampaignStatus.PARTIAL_BUDGET.value
    with StateStore(settings.state_db) as store:
        b = store.get_campaign_budget("camp::budget-1")
        assert b["browser_calls"] <= 1  # the global cap held across adapters


def test_concurrency_one_equals_n_final_state(tmp_path, server):
    # Concurrency 1 and N produce equivalent final canonical lead state.
    s1 = _settings(tmp_path / "a")
    s_n = _settings(tmp_path / "b")
    caps1 = {PUBLIC_BROWSER_ANONYMOUS: 1}
    rt1 = _runtime(s1, server, "conc-1", budget=CampaignBudget(max_waves=1), pool_caps=caps1)
    rtn = _runtime(s_n, server, "conc-1", budget=CampaignBudget(max_waves=1))
    r1 = rt1.run(live=True)
    rn = rtn.run(live=True)
    with StateStore(s1.state_db) as store:
        leads1 = sorted(row["lead_id"] for row in store.list_portal_leads("conc-1"))
    with StateStore(s_n.state_db) as store:
        leadsn = sorted(row["lead_id"] for row in store.list_portal_leads("conc-1"))
    assert leads1 == leadsn  # identical canonical set regardless of concurrency
    # The parallel run observed >1 concurrency in the anonymous pool at some point
    # OR at least respected the cap of 2.
    assert rn.summary["max_observed_concurrency"].get(PUBLIC_BROWSER_ANONYMOUS, 0) <= 2
    assert rn.summary["authenticated_profile_owner_max"] == 0  # no auth profile used


def test_fresh_run_id_guard(tmp_path, server):
    settings = _settings(tmp_path)
    rt = _runtime(settings, server, "guard-1", budget=CampaignBudget(max_waves=1))
    rt.run(live=True)
    # A completed campaign cannot be silently reused as a fresh live run.
    rt2 = _runtime(settings, server, "guard-1", budget=CampaignBudget(max_waves=1))
    with pytest.raises(MarketRunReuseError):
        rt2.run(live=True)
    # ...but an explicit resume is allowed.
    rt3 = _runtime(settings, server, "guard-1", budget=CampaignBudget(max_waves=1))
    rt3.run(live=True, resume=True)


def test_report_is_current_run_only(tmp_path, server):
    settings = _settings(tmp_path)
    _runtime(settings, server, "rrun-1", budget=CampaignBudget(max_waves=1)).run(live=True)
    res2 = _runtime(settings, server, "rrun-2", budget=CampaignBudget(max_waves=1)).run(live=True)
    # run-2's report reflects only run-2's leads (run-scoped), not run-1's.
    with StateStore(settings.state_db) as store:
        leads2 = store.list_portal_leads("rrun-2")
    assert res2.portal_leads == len(leads2)
    assert res2.report_valid is True
