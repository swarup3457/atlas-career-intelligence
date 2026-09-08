"""Phase 1D portal discovery adapter tests (LinkedIn + Naukri, offline fixtures).

Fixture-based regression tests against the CURRENT observed DOM/JSON contracts.
No private page snapshots or session data are committed — the fixtures are
minimal, synthetic reproductions of the public shapes. A local 127.0.0.1 server
serves them, so nothing here touches the live web (these are NOT ``real_web``).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from atlas.models import ErrorCategory
from atlas.sources.adapter import AdapterError
from atlas.sources.health import SourceHealthState
from atlas.sources.models import SearchRequest, SourceFamily, WorkMode
from atlas.sources.portals import PortalJobLead, PortalLeadVerification, make_portal_instance
from atlas.sources.portals.base import canonical_city, city_matches
from atlas.sources.portals.linkedin import LinkedInGuestAdapter
from atlas.sources.portals.naukri import NaukriPublicAdapter

pytestmark = pytest.mark.integration


_LI_OK = """
<li><div class="base-card relative" data-entity-urn="urn:li:jobPosting:3766283014">
<a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/senior-java-backend-engineer-at-acme-3766283014?refId=abc"></a>
<h3 class="base-search-card__title">Senior Java Backend Engineer</h3>
<h4 class="base-search-card__subtitle"><a href="https://www.linkedin.com/company/acme" class="hidden-nested-link">Acme Corp</a></h4>
<span class="job-search-card__location">Bengaluru, Karnataka, India</span>
<time class="job-search-card__listdate" datetime="2026-08-20">3 days ago</time></div></li>
<li><div class="base-card relative" data-entity-urn="urn:li:jobPosting:3766283099">
<a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/software-engineer-3766283099"></a>
<h3 class="base-search-card__title">Software Engineer</h3>
<h4 class="base-search-card__subtitle"><a href="x" class="hidden-nested-link">Globex</a></h4>
<span class="job-search-card__location">Hyderabad, Telangana, India</span>
<time class="job-search-card__listdate--new" datetime="2026-08-25">1 day ago</time></div></li>
<li><div class="base-card relative">
<span class="job-search-card__location">Nowhere</span></div></li>
"""

_LI_AUTHWALL = """<html><body>Join now to see who Acme has hired for this role.
<a href="/authwall">Sign in</a></body></html>"""

_LI_CHALLENGE = """<html><head><title>Just a moment...</title></head>
<body>Checking your browser before you access linkedin.com. Please enable JavaScript and cookies.</body></html>"""

_NAUKRI_OK = {
    "noOfJobs": 2,
    "jobDetails": [
        {
            "jobId": "010101", "title": "Java Backend Developer", "companyName": "Acme India",
            "jdURL": "/job-listings-java-backend-developer-acme-010101",
            "placeholders": [
                {"type": "location", "label": "Bangalore"},
                {"type": "experience", "label": "3-6 Yrs"},
                {"type": "salary", "label": "\u20b9 10-20 LPA"},
            ],
            "footerPlaceholderLabel": "3 Days Ago", "tagsAndSkills": "java,spring,microservices",
        },
        {
            "jobId": "020202", "title": "Software Engineer", "companyName": "Globex",
            "jdURL": "https://www.naukri.com/job-listings-se-globex-020202",
            "placeholders": [
                {"type": "location", "label": "Hyderabad"},
                {"type": "experience", "label": "2-5 Yrs"},
            ],
            "footerPlaceholderLabel": "Just now", "tagsAndSkills": "python",
        },
        {"title": "Ghost With No Id"},
    ],
}

_NAUKRI_BLOCK = "<html><body>Access Denied. Request blocked.</body></html>"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        return

    def _send(self, body, code=200, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        self.server.last_query = self.path  # record for param assertions
        if path == "/li-ok":
            return self._send(_LI_OK)
        if path == "/li-authwall":
            return self._send(_LI_AUTHWALL)
        if path == "/li-challenge":
            return self._send(_LI_CHALLENGE)
        if path == "/li-429":
            return self._send("rate limited", code=429)
        if path == "/naukri-ok":
            return self._send(json.dumps(_NAUKRI_OK), ctype="application/json")
        if path == "/naukri-block":
            return self._send(_NAUKRI_BLOCK, code=403)
        return self._send("not found", code=404)


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.last_query = ""
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd
    httpd.shutdown()


def _base(server):
    return f"http://127.0.0.1:{server.server_address[1]}"


# --- city normalization ------------------------------------------------------
def test_city_normalization_bengaluru_bangalore():
    assert canonical_city("Bengaluru, Karnataka, India") == "bengaluru"
    assert canonical_city("Bangalore") == "bengaluru"
    assert canonical_city("Hyderabad, Telangana") == "hyderabad"
    assert city_matches("Bangalore", "Bengaluru, Karnataka, India")
    assert city_matches("Hyderabad", "Secunderabad")
    assert not city_matches("Bangalore", "Pune, Maharashtra")


# --- LinkedIn ----------------------------------------------------------------
def test_linkedin_parses_cards_with_isolation(server):
    inst = make_portal_instance(SourceFamily.LINKEDIN, "li-1",
                                metadata={"guest_base": f"{_base(server)}/li-ok"})
    adapter = LinkedInGuestAdapter(inst)
    res = adapter.search(SearchRequest(query="java backend", location="Bengaluru", recency_days=7))
    # Two good cards parse; the malformed third is a finding, not a crash.
    assert res.count == 2
    assert res.parse_findings  # the ghost card became a finding
    java = next(r for r in res.results if "Java" in (r.title or ""))
    assert java.source_job_id == "3766283014"
    assert java.canonical_url == "https://www.linkedin.com/jobs/view/3766283014"
    assert java.company == "Acme Corp"
    assert "Bengaluru" in (java.location or "")
    assert java.posted_at == "2026-08-20"


def test_linkedin_recency_param_present(server):
    inst = make_portal_instance(SourceFamily.LINKEDIN, "li-2",
                                metadata={"guest_base": f"{_base(server)}/li-ok"})
    adapter = LinkedInGuestAdapter(inst)
    adapter.search(SearchRequest(query="java", location="Bengaluru", recency_days=7))
    # f_TPR must be present and correct (7 days = 604800s) — never silently omitted.
    assert "f_TPR=r604800" in server.last_query


def test_linkedin_authwall_is_login_required(server):
    inst = make_portal_instance(SourceFamily.LINKEDIN, "li-3",
                                metadata={"guest_base": f"{_base(server)}/li-authwall"})
    adapter = LinkedInGuestAdapter(inst)
    with pytest.raises(AdapterError) as exc:
        adapter.search(SearchRequest(query="java", location="Bengaluru"))
    assert exc.value.category == ErrorCategory.LOGIN_WALL


def test_linkedin_challenge_is_access_limited(server):
    inst = make_portal_instance(SourceFamily.LINKEDIN, "li-4",
                                metadata={"guest_base": f"{_base(server)}/li-challenge"})
    adapter = LinkedInGuestAdapter(inst)
    with pytest.raises(AdapterError) as exc:
        adapter.search(SearchRequest(query="java", location="Bengaluru"))
    assert exc.value.category == ErrorCategory.ANTI_BOT


def test_linkedin_rate_limited(server):
    inst = make_portal_instance(SourceFamily.LINKEDIN, "li-5",
                                metadata={"guest_base": f"{_base(server)}/li-429"})
    adapter = LinkedInGuestAdapter(inst)
    with pytest.raises(AdapterError) as exc:
        adapter.search(SearchRequest(query="java", location="Bengaluru"))
    assert exc.value.category == ErrorCategory.HTTP_429


def test_linkedin_authenticated_route_concurrency_is_one():
    inst = make_portal_instance(SourceFamily.LINKEDIN, "li-auth",
                                auth_ref="ATLAS_LINKEDIN_PROFILE",
                                metadata={"guest_base": "http://127.0.0.1/x"})
    adapter = LinkedInGuestAdapter(inst)
    from atlas.sources.models import ConcurrencyClass

    assert adapter.concurrency_class == ConcurrencyClass.BROWSER_AUTHENTICATED


# --- Naukri ------------------------------------------------------------------
def test_naukri_parses_jobs_with_india_fields(server):
    inst = make_portal_instance(SourceFamily.NAUKRI, "nk-1",
                                metadata={"search_base": f"{_base(server)}/naukri-ok"})
    adapter = NaukriPublicAdapter(inst)
    res = adapter.search(SearchRequest(query="java", location="Bengaluru", recency_days=7))
    assert res.count == 2  # ghost element skipped
    java = next(r for r in res.results if "Java" in (r.title or ""))
    assert java.source_job_id == "010101"
    assert java.company == "Acme India"
    assert "Bangalore" in (java.location or "")
    assert java.salary_text and "LPA" in java.salary_text  # LPA/INR preserved
    assert java.experience_text == "3-6 Yrs"
    assert java.canonical_url.startswith("https://www.naukri.com/job-listings-")


def test_naukri_recency_param_present(server):
    inst = make_portal_instance(SourceFamily.NAUKRI, "nk-2",
                                metadata={"search_base": f"{_base(server)}/naukri-ok"})
    adapter = NaukriPublicAdapter(inst)
    adapter.search(SearchRequest(query="java", location="Bengaluru", recency_days=7))
    assert "jobAge=7" in server.last_query


def test_naukri_block_is_access_limited(server):
    inst = make_portal_instance(SourceFamily.NAUKRI, "nk-3",
                                metadata={"search_base": f"{_base(server)}/naukri-block"})
    adapter = NaukriPublicAdapter(inst)
    with pytest.raises(AdapterError) as exc:
        adapter.search(SearchRequest(query="java", location="Bengaluru"))
    assert exc.value.category == ErrorCategory.ANTI_BOT


# --- PortalJobLead model -----------------------------------------------------
def test_portal_lead_is_never_verified_official(server):
    inst = make_portal_instance(SourceFamily.LINKEDIN, "li-lead",
                                metadata={"guest_base": f"{_base(server)}/li-ok"})
    adapter = LinkedInGuestAdapter(inst)
    res = adapter.search(SearchRequest(query="java", location="Bengaluru"))
    lead = PortalJobLead.from_discovery_result(
        res.results[0], run_id="r-1", source_family="linkedin", lane="JAVA_BACKEND",
        result_query="java", result_page=1,
    )
    assert lead.verification_state == PortalLeadVerification.PORTAL_CURRENT_LEAD
    assert lead.has_min_identity()
    assert lead.lead_id.startswith("lead::r-1::linkedin::")


def test_portal_lead_ghost_lacks_min_identity():
    lead = PortalJobLead(run_id="r-1", source_family="linkedin", title=None, portal_job_id=None)
    assert not lead.has_min_identity()


def test_portal_lead_persist_roundtrip(tmp_path, server):
    from atlas.persistence.sqlite import StateStore

    inst = make_portal_instance(SourceFamily.NAUKRI, "nk-lead",
                                metadata={"search_base": f"{_base(server)}/naukri-ok"})
    adapter = NaukriPublicAdapter(inst)
    res = adapter.search(SearchRequest(query="java", location="Bengaluru"))
    with StateStore(tmp_path / "s.sqlite") as store:
        for r in res.results:
            lead = PortalJobLead.from_discovery_result(
                r, run_id="run-x", source_family="naukri", lane="JAVA_BACKEND",
            )
            lead.persist(store)
        rows = store.list_portal_leads("run-x")
        assert len(rows) == 2
        assert all(row["verification_state"] == "PORTAL_CURRENT_LEAD" for row in rows)
        loaded = PortalJobLead.from_row(rows[0])
        assert loaded.source_family == "naukri"
