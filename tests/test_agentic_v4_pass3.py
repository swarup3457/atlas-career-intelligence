"""Agentic V4 pass-3 — robust browser search + honest access classification (prompt s.3.4, s.10).

Fixes the pass-2 live failures where enterprise SPAs were mislabeled ACCESS_LIMITED:
* a plain-HTTP (non-JS) discovery 403 alone must NOT terminate a company — the stateful
  browser is the arbiter (prompt s.3.4);
* a fragile Search-button click that times out is a SOFT failure that never aborts a lane
  (Enter / query-param URL still search);
* a BROWSER-confirmed hard navigation block (net::ERR / HTTP 403 in the browser) is a
  truthful ACCESS_LIMITED_EXTERNAL.

Uses a REAL headless Chrome against a local query-param SPA whose Search button is covered by
an invisible overlay (so a direct click times out), served on 127.0.0.1 (offline, `browser`).
"""

from __future__ import annotations

import functools
import http.server
import threading
from pathlib import Path

import pytest

from atlas.pilot.agentic_tools import AgenticCompanyToolbox
from atlas.pilot.browser_actor import CompanyBrowserActor, is_hard_navigation_block
from atlas.pilot.config import load_pilot_config
from atlas.pilot.status_v4 import (
    CompanySearchStatus,
    is_genuinely_searched,
    is_internal_retryable,
    is_terminal,
)

_SPA2 = Path(__file__).resolve().parents[1] / "fixtures" / "agentic_v4" / "spa2"


@pytest.fixture(scope="module")
def config():
    return load_pilot_config()


@pytest.fixture(scope="module")
def spa2_server():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(_SPA2))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}/index.html"
    finally:
        httpd.shutdown()


def _factory(**kw):
    kw.setdefault("action_timeout_s", 30.0)
    kw.setdefault("nav_timeout_ms", 8000)
    return CompanyBrowserActor(allow_http_hosts=("127.0.0.1",), **kw)


# --- unit: hard-nav classification ----------------------------------------
def test_hard_navigation_block_classifier():
    assert is_hard_navigation_block("Page.goto: net::ERR_HTTP_RESPONSE_CODE_FAILURE at https://x")
    assert is_hard_navigation_block("net::ERR_CONNECTION_REFUSED")
    assert not is_hard_navigation_block("Locator.click: Timeout 15000ms exceeded")
    assert not is_hard_navigation_block("")


# --- plain-HTTP 403 alone must not terminate the company ------------------
def test_http_403_alone_is_not_terminal_block(config):
    tb = AgenticCompanyToolbox(company="Acme", config=config, task_id="t")
    tb._http_access_limited = True   # discovery saw a non-JS 403
    # nothing searched, browser never started -> honest ACCESS_LIMITED_EXTERNAL,
    # but this is NOT an internal error and is only from the weak HTTP signal
    status = tb._v4_status()
    assert status == CompanySearchStatus.ACCESS_LIMITED_EXTERNAL.value
    # the key property: it did not become a browser/async internal error
    assert not is_internal_retryable(status)


def test_soft_browser_error_is_retryable_incomplete(config):
    tb = AgenticCompanyToolbox(company="Acme", config=config, task_id="t")
    tb._browser_started = True
    tb._browser_error = True          # a click timeout
    tb._browser_searched = False
    status = tb._v4_status()
    assert status == CompanySearchStatus.BROWSER_TOOL_ERROR.value
    assert is_internal_retryable(status) and not is_terminal(status)


def test_browser_confirmed_block_is_access_limited(config):
    tb = AgenticCompanyToolbox(company="Acme", config=config, task_id="t")
    tb._browser_started = True
    tb._browser_access_limited = True   # the browser itself was refused
    status = tb._v4_status()
    assert status == CompanySearchStatus.ACCESS_LIMITED_EXTERNAL.value
    assert is_terminal(status) and not is_genuinely_searched(status)


# --- real browser: goto_search + robust lane search on an overlay SPA -----
@pytest.mark.browser
def test_goto_search_query_param_filters_results(spa2_server, config):
    tb = AgenticCompanyToolbox(company="Globex", config=config, task_id="t1", browser_factory=_factory)
    try:
        tb.browser_start(spa2_server)
        # the SPA filters on ?q=&loc= — a direct search URL returns India Java jobs
        out = tb.browser_goto_search(spa2_server + "?q=Java&loc=India")
        assert out["ok"] is True
        cards = tb.browser_collect_job_cards(lane="JAVA_BACKEND")
        titles = [c["title"] for c in cards.get("job_cards", [])]
        assert any("java" in t.lower() for t in titles)
        assert all("india" in (c["location"] + c["url"]).lower() or c["location"] for c in cards["job_cards"])
    finally:
        tb.cleanup()


@pytest.mark.browser
def test_robust_lane_search_survives_overlay_click_timeout(spa2_server, config):
    tb = AgenticCompanyToolbox(company="Globex", config=config, task_id="t2", browser_factory=_factory)
    try:
        tb.browser_start(spa2_server)
        # the Search button is covered by an overlay (a click would time out); the
        # robust lane search uses Enter and still observes results, so the lane is
        # genuinely attempted and NOT aborted.
        res = tb.browser_search_lane("JAVA_BACKEND", "Java", "India")
        assert res["observed"] is True
        assert res["count"] >= 1
        assert tb.base.lanes["JAVA_BACKEND"].attempted is True
        # a soft click timeout did not flip the company into a terminal block
        assert not tb._browser_access_limited
    finally:
        tb.cleanup()


@pytest.mark.browser
def test_deterministic_lane_completion_finishes_checklist(spa2_server, config):
    """The LLM may submit after 1 lane; Python's deterministic completion pass
    finishes the remaining lanes on the still-open browser -> genuinely searched."""
    from atlas.pilot.agentic_worker import AgenticCompanySearchWorker, AgenticCompanyTask
    from atlas.pilot.usage import UsageMeter

    worker = AgenticCompanySearchWorker(config=config, mode="deterministic", headless=True,
                                        browser_factory=_factory)
    tb = worker._toolbox(AgenticCompanyTask(company="Globex", task_id="t"))
    # a browser-only discovery result (no ATS): the deterministic completion runs
    from atlas.pilot.discovery import DiscoveryResult
    tb.base.discovery = DiscoveryResult(company="Globex", official_domain="127.0.0.1",
                                        career_entry_url=spa2_server, route="GENERIC_BROWSER",
                                        status="UNSUPPORTED_SITE")
    tb.browser_start(spa2_server)
    # simulate the LLM having covered only ONE lane then stopping
    tb.browser_search_lane("JAVA_BACKEND", "Java", "India")
    assert not tb.base.lanes["DOTNET"].attempted
    # Python completes the remaining lanes deterministically on the open browser
    worker._complete_lanes_deterministically(tb)
    try:
        assert all(tb.base.lanes[l].attempted for l in config.primary_lanes)
        out = tb.submit_company_search_result()
        assert out["lanes_complete"] is True
        assert is_genuinely_searched(out["status"])
    finally:
        tb.cleanup()
