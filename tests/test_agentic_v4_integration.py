"""Agentic V4 — end-to-end tool-surface wiring (prompt s.4, s.6, s.10).

Proves the NEW integration (built in pass 2): the AgenticCompanyToolbox exposes the twelve
stateful browser tools + convenience lane search, captures typed JobDetailEvidence from a
browser-opened detail, and submits a CompanySearchResult with a correct V4 status. Uses a
REAL headless Chrome against the local SPA fixture (offline, `browser` marker) plus a fake
actor for the internal-error path.
"""

from __future__ import annotations

import functools
import http.server
import threading
from pathlib import Path

import pytest

from atlas.pilot.agentic_tools import AgenticCompanyToolbox, V4_TOOL_NAMES, build_v4_sdk_tools
from atlas.pilot.browser_actor import BrowserActorError, CompanyBrowserActor
from atlas.pilot.config import load_pilot_config
from atlas.pilot.status_v4 import CompanySearchStatus, is_genuinely_searched, is_internal_retryable, is_terminal

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "agentic_v4" / "spa"


@pytest.fixture(scope="module")
def config():
    return load_pilot_config()


@pytest.fixture(scope="module")
def spa_server():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(_FIXTURE_DIR))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}/index.html"
    finally:
        httpd.shutdown()


def _spa_factory(**kw):
    kw.setdefault("action_timeout_s", 30.0)
    kw.setdefault("nav_timeout_ms", 8000)
    return CompanyBrowserActor(allow_http_hosts=("127.0.0.1",), **kw)


@pytest.mark.browser
def test_toolbox_browser_surface_end_to_end(spa_server, config):
    tb = AgenticCompanyToolbox(company="Acme", config=config, task_id="t1",
                              browser_factory=_spa_factory)
    try:
        start = tb.browser_start(spa_server)
        assert start["ok"] is True
        # cover all five lanes via the convenience search; JAVA yields India cards
        opened = 0
        for lane in config.primary_lanes:
            q = (config.query_for(lane) or ["Java"])[0]
            res = tb.browser_search_lane(lane, q, "India")
            assert res["lane"] == lane
            if res.get("cards") and opened < 2:
                # open the first India card as a job detail and capture typed evidence
                detail = tb.browser_open_job_detail(handle=res["cards"][0]["handle"], lane_hint=lane)
                assert detail["ok"] is True
                tb.browser_back()
                opened += 1
        assert opened >= 1
        # typed evidence captured into the shared details list
        assert len(tb.base.details) >= 1
        j = tb.base.details[0]
        assert j.title and j.description and ("india" in j.location.lower() or j.location)
        # all five lanes attempted -> genuinely searched, with matches
        out = tb.submit_company_search_result()
        assert out["status"] == CompanySearchStatus.SEARCHED_COMPLETE_WITH_MATCHES.value
        assert is_genuinely_searched(out["status"])
        assert out["lanes_complete"] is True
        assert out["jobs"] >= 1
    finally:
        tb.cleanup()


def test_v4_tool_surface_names_and_count(config):
    tb = AgenticCompanyToolbox(company="Acme", config=config)
    names = {t.name for t in build_v4_sdk_tools(tb)}
    assert names == set(V4_TOOL_NAMES)
    assert len(names) == 21
    assert "browser_goto_search" in names
    # the terminal tool is submit
    tb.cleanup()


class _BoomActor:
    def __init__(self, **kw):
        self.action_count = 0

    def start(self, url):
        self.action_count += 1
        raise BrowserActorError("PlaywrightError: simulated tool crash")

    def close(self):
        return {"ok": True}


def test_internal_browser_error_is_retryable_not_searched(config):
    tb = AgenticCompanyToolbox(company="Acme", config=config, task_id="t2",
                              browser_factory=lambda **kw: _BoomActor(**kw))
    out = tb.browser_start("https://careers.acme.com/search")
    assert out["ok"] is False and out.get("retryable") is True
    sub = tb.submit_company_search_result()
    status = sub["status"]
    assert status == CompanySearchStatus.BROWSER_TOOL_ERROR.value
    assert is_internal_retryable(status)
    assert not is_terminal(status)
    assert not is_genuinely_searched(status)
    tb.cleanup()


def test_web_leads_and_fetch_bound_to_official_domain(config):
    tb = AgenticCompanyToolbox(company="Acme", config=config)
    tb.resolve_official_company_site("Accenture")  # sets official domain
    # web tools refuse an untrusted host regardless of what the model passes
    out = tb.web_fetch_official("https://randomjobboard.example/job/1")
    assert "error" in out


def test_non_job_banner_titles_rejected():
    from atlas.pilot.agentic_tools import looks_like_job_title
    # real postings
    assert looks_like_job_title("Senior Java Full Stack Developer")
    assert looks_like_job_title("Software Development Engineering - Sr Professional I")
    assert looks_like_job_title(".NET Core Dev with SQL and Azure || Pune")
    # banners / non-jobs the live browser path can accidentally capture
    assert not looks_like_job_title("YOU ARE ONE STEP CLOSER TO FINDING YOUR NEXT JOB")
    assert not looks_like_job_title("IBM")
    assert not looks_like_job_title("Taulia careers")
    assert not looks_like_job_title("")
    assert not looks_like_job_title("Search jobs by title")


def test_thin_capture_rejected(config):
    """A job-like title with nav-junk detail text (e.g. 'Email X LinkedIn') must
    NOT be recorded as evidence — it is thin/low-signal, not a real JD."""
    tb = AgenticCompanyToolbox(company="IBM", config=config, task_id="t")
    tb._capture_detail({"detail_text": "Email X LinkedIn", "url": "https://careers.ibm.com/job/1",
                        "headings": []}, card={"title": "Application Developer FullStack", "location": ""})
    assert len(tb.base.details) == 0
    # a real JD with content markers IS recorded
    tb._capture_detail({"detail_text": "Java Backend Engineer. Responsibilities: build Spring Boot "
                        "microservices and REST APIs. Required qualifications: 2 years experience in "
                        "Java development. Skills: Java, Spring, SQL. Location Bengaluru, India.",
                        "url": "https://careers.ibm.com/job/2", "headings": []},
                       card={"title": "Java Backend Engineer", "location": "Bengaluru, India"})
    assert len(tb.base.details) == 1
