"""Phase 1C-B Layer C integration tests: GenericCareerBrowserAdapter (headless).

Real Chrome (headless, offline) driven against deterministic LOCAL pages served
on 127.0.0.1. Covers SPA delayed rendering, a search form, load-more, bounded
infinite scroll, a job-card selector (dynamic cards / detail panel), a stale
selector falling back gracefully, a challenge page (classified, never bypassed),
and a navigation failure (browser crash/restart resilience).

No internet, no visible browser window. Marked ``browser`` (offline) so it runs
in the default gate but never as ``real_web``.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from atlas.models import ErrorCategory
from atlas.sources.adapter import AdapterError
from atlas.sources.generic.browser_adapter import GenericCareerBrowserAdapter
from atlas.sources.models import SearchRequest, SourceFamily, SourceInstance, SourceType

pytestmark = [pytest.mark.integration, pytest.mark.browser]


_SPA_DELAYED = """<html><body><div id="root"></div>
<script>setTimeout(function(){document.getElementById('root').innerHTML=
'<a href="/jobs/1">Java Backend Engineer</a><a href="/jobs/2">Frontend Engineer</a>';}, 150);</script>
</body></html>"""

_SEARCH_FORM = """<html><body>
<input id="q" type="search" placeholder="Search jobs"/>
<div id="results"></div>
<script>
document.getElementById('q').addEventListener('keydown', function(e){
  if(e.key==='Enter'){document.getElementById('results').innerHTML=
   '<a href="/jobs/1">Java Backend Engineer</a><a href="/jobs/2">Platform Engineer</a>';}
});
</script></body></html>"""

_LOAD_MORE = """<html><body>
<div id="results"><a href="/jobs/1">Role 1</a><a href="/jobs/2">Role 2</a></div>
<button id="more">Load more</button>
<script>
var n=2;
document.getElementById('more').addEventListener('click', function(){
  n++; var a=document.createElement('a'); a.href='/jobs/'+n; a.textContent='Role '+n;
  document.getElementById('results').appendChild(a);
});
</script></body></html>"""

_INFINITE = """<html><body>
<div id="results"><a href="/jobs/1">Role 1</a></div>
<div style="height:4000px"></div>
<script>
var n=1;
window.addEventListener('scroll', function(){
  if(window.scrollY>50 && n<6){n++; var a=document.createElement('a'); a.href='/jobs/'+n;
   a.textContent='Role '+n; document.getElementById('results').appendChild(a);}
});
</script></body></html>"""

_CARDS = """<html><body>
<div class="joblist">
  <div class="card"><a href="/jobs/1">Card One Engineer</a><span>Bengaluru</span></div>
  <div class="card"><a href="/jobs/2">Card Two Engineer</a><span>Hyderabad</span></div>
</div></body></html>"""

_ANCHORS_ONLY = """<html><body>
<a href="/jobs/1">Anchor Role One</a>
<a href="/jobs/2">Anchor Role Two</a>
</body></html>"""

_CHALLENGE = """<html><head><title>Just a moment...</title></head>
<body>Please verify you are a human. Checking your browser before accessing.</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        return

    def _send(self, body, code=200):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        routes = {
            "/spa": _SPA_DELAYED, "/search": _SEARCH_FORM, "/loadmore": _LOAD_MORE,
            "/infinite": _INFINITE, "/cards": _CARDS, "/anchors": _ANCHORS_ONLY,
            "/challenge": _CHALLENGE,
        }
        path = self.path.split("?")[0]
        if path in routes:
            return self._send(routes[path])
        if path.startswith("/jobs/"):
            return self._send("<html><body>job detail</body></html>")
        return self._send("<html><body>not found</body></html>", code=404)


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture()
def manager(tmp_path):
    from atlas.browser.manager import BrowserManager

    mgr = BrowserManager(tmp_path / "careers-profile", channel="chrome")
    mgr.launch(headless=True)
    yield mgr
    mgr.close()


def _adapter(manager, entry: str, **md) -> GenericCareerBrowserAdapter:
    metadata = {"entry_url": entry, "render_wait_ms": 700, "max_load_cycles": 2}
    recipe = md.pop("recipe", None)
    if recipe is not None:
        metadata["recipe"] = recipe
    metadata.update(md)
    inst = SourceInstance(
        instance_id="b-fix", source_type=SourceType.COMPANY_CAREER,
        source_family=SourceFamily.COMPANY_CAREER_BROWSER, display_name="Fixture", metadata=metadata,
    )
    return GenericCareerBrowserAdapter(inst, browser_manager=manager, headless=True)


def test_spa_delayed_rendering(server, manager):
    a = _adapter(manager, f"{server}/spa")
    r = a.search(SearchRequest(query="java"))
    assert r.count == 2
    assert {x.title for x in r.results} == {"Java Backend Engineer", "Frontend Engineer"}
    assert all(x.verification_level.value == "OFFICIAL_SEARCH_LIVE" for x in r.results)


def test_search_form_submit(server, manager):
    a = _adapter(manager, f"{server}/search")
    r = a.search(SearchRequest(query="java"))
    titles = {x.title for x in r.results}
    assert "Java Backend Engineer" in titles


def test_load_more_button(server, manager):
    a = _adapter(manager, f"{server}/loadmore", max_load_cycles=2)
    r = a.search(SearchRequest(query=None))
    # 2 initial + up to 2 appended.
    assert r.count > 2


def test_bounded_infinite_scroll(server, manager):
    a = _adapter(manager, f"{server}/infinite", max_load_cycles=2)
    r = a.search(SearchRequest(query=None))
    assert r.count >= 2  # scrolling appended more cards, bounded by cycles


def test_job_card_selector(server, manager):
    a = _adapter(manager, f"{server}/cards", recipe={"job_card_selector": ".card"})
    r = a.search(SearchRequest(query=None))
    assert r.count == 2
    assert {x.title for x in r.results} == {"Card One Engineer", "Card Two Engineer"}


def test_stale_selector_falls_back_to_anchors(server, manager):
    # A recipe with a selector that no longer matches must not zero out — the
    # adapter falls back to anchor extraction and still finds jobs.
    a = _adapter(manager, f"{server}/anchors", recipe={"job_card_selector": ".does-not-exist"})
    r = a.search(SearchRequest(query=None))
    assert r.count == 2


def test_challenge_page_classified_not_bypassed(server, manager):
    a = _adapter(manager, f"{server}/challenge")
    with pytest.raises(AdapterError) as ei:
        a.search(SearchRequest(query=None))
    assert ei.value.category == ErrorCategory.ANTI_BOT


def test_navigation_failure_surfaces_error(server, manager):
    # A dead endpoint surfaces a navigation error, not a false "no jobs".
    a = _adapter(manager, "http://127.0.0.1:1/dead")
    with pytest.raises(AdapterError) as ei:
        a.search(SearchRequest(query=None))
    assert ei.value.category in (ErrorCategory.TRANSIENT_NAVIGATION, ErrorCategory.SOURCE_UNAVAILABLE)
    # Restart the browser (close + relaunch the SAME manager) and confirm it
    # works — crash/restart resilience with no leaked profile lock.
    manager.close()
    manager.launch(headless=True)
    ok = _adapter(manager, f"{server}/spa")
    assert ok.search(SearchRequest(query="java")).count == 2


def test_health_check_healthy_and_access_limited(server, manager):
    from atlas.sources.health import SourceHealthState

    assert _adapter(manager, f"{server}/spa").health_check().state == SourceHealthState.HEALTHY
    assert _adapter(manager, f"{server}/challenge").health_check().state == SourceHealthState.ACCESS_LIMITED
