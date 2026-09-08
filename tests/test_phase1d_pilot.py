"""Phase 1D end-to-end market pilot (offline, deterministic + headless browser).

Wires the COMPLETE market capability under ONE sealed run against local 127.0.0.1
fixtures: official coverage (generic HTTP + generic browser routes) through the
ONE production governor, READ-ONLY LinkedIn/Naukri portal discovery, dynamic
company registration, portal->official verification (a portal lead is linked to
matching official evidence), bounded concurrency (single report writer, no
authenticated profile), and ONE current-run report. This is the deterministic
proof of the §14 pilot mechanisms; the live canary (real_web) proves the live
portions truthfully.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from atlas.careers.pilot import PilotCompany
from atlas.config import load_settings
from atlas.market.campaign import CampaignBudget
from atlas.market.pilot import MarketPilot, MarketPilotConfig
from atlas.market.pools import PUBLIC_BROWSER_ANONYMOUS
from atlas.persistence.sqlite import StateStore

pytestmark = [pytest.mark.integration, pytest.mark.browser]


def _acme_careers(host: str) -> str:
    return ("<html><head><title>Acme Careers</title>"
            "<script type=\"application/ld+json\">[{\"@type\":\"JobPosting\","
            "\"title\":\"Java Backend Engineer\",\"datePosted\":\"2026-08-01\","
            "\"url\":\"http://HOST/jobs/acme-1\",\"identifier\":\"acme-1\","
            "\"hiringOrganization\":{\"name\":\"Acme India\"},"
            "\"jobLocation\":{\"address\":{\"addressLocality\":\"Bengaluru\",\"addressCountry\":\"IN\"}}}]</script>"
            "</head><body>Acme engineering careers</body></html>").replace("HOST", host)


_BROWSER_CAREERS = """<html><body><div id="root"></div>
<script>setTimeout(function(){document.getElementById('root').innerHTML=
'<a href="/jobs/b-1">Senior Software Engineer</a><a href="/jobs/b-2">Backend Engineer</a>';}, 100);</script>
</body></html>"""

_LI = ("<li><div class=\"base-card\" data-entity-urn=\"urn:li:jobPosting:900\">"
       "<a class=\"base-card__full-link\" href=\"https://www.linkedin.com/jobs/view/900\"></a>"
       "<h3 class=\"base-search-card__title\">Java Backend Engineer</h3>"
       "<h4 class=\"base-search-card__subtitle\"><a href=\"x\">Acme India</a></h4>"
       "<span class=\"job-search-card__location\">Bengaluru, Karnataka, India</span>"
       "<time class=\"job-search-card__listdate\" datetime=\"2026-08-20\">3 days ago</time></div></li>")

_NK = {"noOfJobs": 1, "jobDetails": [{
    "jobId": "nk900", "title": "Backend Developer", "companyName": "Globex India",
    "jdURL": "/job-listings-be-globex-nk900",
    "placeholders": [{"type": "location", "label": "Hyderabad"}, {"type": "experience", "label": "3-6 Yrs"},
                     {"type": "salary", "label": "\u20b9 12-22 LPA"}],
    "footerPlaceholderLabel": "1 Day Ago", "tagsAndSkills": "java,spring"}]}


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
        host = f"127.0.0.1:{self.server.server_address[1]}"
        if p == "/acme-careers":
            return self._send(_acme_careers(host))
        if p == "/browser-careers":
            return self._send(_BROWSER_CAREERS)
        if p == "/missing-careers":
            return self._send("<html><body>not found</body></html>", code=404)
        if p == "/li":
            return self._send(_LI)
        if p == "/nk":
            return self._send(json.dumps(_NK), ctype="application/json")
        return self._send("<html><body>home</body></html>")


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


def test_market_pilot_end_to_end(tmp_path, server):
    settings = _settings(tmp_path)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    host = f"127.0.0.1:{server.server_address[1]}"
    config = MarketPilotConfig(
        run_id="mpilot-1",
        official_companies=[
            PilotCompany("co-acme", "Acme India", official_domain=host,
                         known_careers_url=f"{base}/acme-careers", expected_route="GENERIC_HTTP"),
            PilotCompany("co-browser", "BrowserCo", official_domain=host,
                         known_careers_url=f"{base}/browser-careers", expected_route="GENERIC_BROWSER"),
            PilotCompany("co-missing", "MissingCo", official_domain=host,
                         known_careers_url=f"{base}/missing-careers"),
        ],
        lanes=["JAVA_BACKEND", "GENERAL_SOFTWARE"], geography_groups=["PRIMARY"], max_pages=1, max_cards=20)
    pilot = MarketPilot(
        settings, config, budget=CampaignBudget(max_waves=1, max_browser_calls=20, max_portal_pages=1),
        portal_metadata={"linkedin": {"guest_base": f"{base}/li"}, "naukri": {"search_base": f"{base}/nk"}},
        browser_profile_dir=tmp_path / "cprofile")
    result = pilot.run(live=True)

    # B: portal discovery returned valid leads (LinkedIn + Naukri).
    assert result.portal_leads >= 2, result.to_dict()
    # A / E: official coverage produced current-run rows (generic HTTP + browser).
    assert result.official_reported >= 1
    # C: dynamic company registered + at least one portal lead linked to official.
    assert result.dynamic_companies >= 1
    assert result.verified_links >= 1, "no portal lead linked to official evidence"
    # D: one report writer, no authenticated profile owner, pool caps observed.
    assert result.report_valid is True
    assert result.auth_profile_owner_max == 0
    assert result.max_concurrency.get(PUBLIC_BROWSER_ANONYMOUS, 0) <= 2

    with StateStore(settings.state_db) as store:
        # §14A: the nonexistent /missing-careers path is NOT a validated entry.
        missing = store.list_career_entry_points(company_id="co-missing")
        assert missing and all(row["validated"] == 0 for row in missing)
        # The linked lead is verified official (portal was NOT auto-official).
        links = store.list_portal_official_links("mpilot-1")
        verified = [l for l in links if l["verification_state"] == "LINKED_OFFICIAL_VERIFIED"]
        assert verified, "expected at least one LINKED_OFFICIAL_VERIFIED"
        # The report exists and is current-run scoped.
        leads = store.list_portal_leads("mpilot-1")
        assert len(leads) == result.portal_leads
