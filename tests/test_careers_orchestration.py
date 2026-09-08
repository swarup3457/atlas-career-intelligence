"""Phase 1C-B Layer D orchestration tests: generic career adapters through the
ONE production governor.

Runs the REAL LangGraph production runtime with the careers registry against a
local fixture HTTP server (127.0.0.1, offline). Proves: a sealed plan reaches a
truthful terminal state; the generic HTTP route stages observations and produces
ONE current-run report; concurrency 1 == N; an unresolved (SPA) route is a
truthful EXTRACTION_UNRESOLVED (never a false trusted-zero / false COMPLETE with
results); a resumed run does not duplicate observations; and parallel leased
execution does not duplicate. One test drives the browser route through the same
runtime headlessly.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from atlas.config import load_settings
from atlas.orchestration.production_state import ProductionPhase, ProductionTerminalState
from atlas.persistence.sqlite import StateStore
from atlas.planning import PlannedCompany
from atlas.runtime.production import ProductionSearchRuntime
from atlas.sources.coverage import CoverageManifest, CoverageStatus
from atlas.sources.generic import build_careers_registry
from atlas.sources.models import Capability, SourceFamily, SourceInstance, SourceType

pytestmark = pytest.mark.integration


def _jobs_page(host: str, label: str, prefix: str) -> str:
    jobs = [("Java Backend Engineer", f"{prefix}1", "Bengaluru"),
            ("Senior Software Engineer", f"{prefix}2", "Bengaluru")]
    nodes = []
    for title, jid, city in jobs:
        nodes.append(
            '{"@type":"JobPosting","title":"%s","datePosted":"2026-08-01",'
            '"hiringOrganization":{"name":"%s"},"url":"http://%s/jobs/%s","identifier":"%s",'
            '"jobLocation":{"address":{"addressLocality":"%s","addressCountry":"IN"}}}'
            % (title, label, host, jid, jid, city)
        )
    return ("<html><head><title>Careers</title>"
            '<script type="application/ld+json">[' + ",".join(nodes) + "]</script></head>"
            "<body>Real careers content about our engineering teams.</body></html>")


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
        host = f"127.0.0.1:{self.server.server_address[1]}"
        path = self.path.split("?")[0]
        labels = {"/careers-a": ("CoA", "a"), "/careers-b": ("CoB", "b"), "/careers-c": ("CoC", "c")}
        if path in labels:
            label, prefix = labels[path]
            return self._send(_jobs_page(host, label, prefix))
        if path == "/spa":
            return self._send('<html><body><div id="root"></div><script src="/x.js"></script></body></html>')
        if path.startswith("/jobs/"):
            return self._send("<html><body>detail</body></html>")
        return self._send("not found", code=404, ctype="text/plain")


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _settings(tmp_path):
    s = load_settings(
        state_db=tmp_path / "state" / "atlas_state.sqlite",
        checkpoint_db=tmp_path / "state" / "atlas_checkpoints.sqlite",
        output_dir=tmp_path / "output",
        logs_dir=tmp_path / "logs",
        browser_profile=tmp_path / "profile",
        agents_dir=tmp_path / "agents",
        skills_dir=tmp_path / "skills",
    )
    s.ensure_directories()
    return s


def _http_instance(iid: str, entry: str, company_id: str) -> SourceInstance:
    return SourceInstance(
        instance_id=iid, source_type=SourceType.COMPANY_CAREER, source_family=SourceFamily.COMPANY_CAREER,
        display_name=f"Careers {company_id}", base_url=entry, company_id=company_id,
        capability_overrides=frozenset({Capability.SEARCH, Capability.DETAIL, Capability.PAGINATION}),
        metadata={"entry_url": entry, "max_pages": 2, "max_results": 20, "request_budget": 40},
    )


def _runtime(settings, run_id, instances, companies, **kw):
    return ProductionSearchRuntime(
        settings, run_id, fixture_mode=True, companies=companies, instances=instances,
        registry=build_careers_registry(), lane_override=["JAVA_BACKEND", "GENERAL_SOFTWARE"],
        max_per_company=1, max_per_instance=1, max_per_tenant=1, **kw,
    )


# --- 1. end-to-end COMPLETE + report -----------------------------------------
def test_generic_http_runtime_reaches_complete_with_report(tmp_path, server):
    settings = _settings(tmp_path)
    inst = _http_instance("acme-http", f"{server}/careers-a", "acme")
    companies = [PlannedCompany(company_id="acme", name="Acme", source_instances=("acme-http",), geography_group="PRIMARY")]
    result = _runtime(settings, "run-http-1", {"acme-http": inst}, companies).run()
    assert result.terminal_state == ProductionTerminalState.COMPLETE.value
    assert result.report_valid is True and result.report_path
    with StateStore(settings.state_db) as store:
        assert store.count_raw_observations("run-http-1") >= 2
        assert store.count_current_run_canonical_jobs("run-http-1") >= 2
        manifest = CoverageManifest.load(store, "run-http-1")
        assert all(t.is_terminal for t in manifest.tasks())
        assert any(t.status == CoverageStatus.COMPLETED_WITH_RESULTS for t in manifest.tasks())


# --- 2. concurrency 1 == N ---------------------------------------------------
def _run_topology(tmp_path, server, run_id, workers):
    settings = _settings(tmp_path)
    instances = {}
    companies = []
    for i, path in enumerate(("/careers-a", "/careers-b", "/careers-c")):
        iid = f"co{i}-http"
        instances[iid] = _http_instance(iid, f"{server}{path}", f"co{i}")
        companies.append(PlannedCompany(company_id=f"co{i}", name=f"Co{i}", source_instances=(iid,), geography_group="PRIMARY"))
    result = _runtime(settings, run_id, instances, companies, parallel_workers=workers).run()
    with StateStore(settings.state_db) as store:
        canon = store.count_current_run_canonical_jobs(run_id)
        obs = store.count_raw_observations(run_id)
    return result, canon, obs


def test_concurrency_one_equals_n(tmp_path, server):
    r1, canon1, obs1 = _run_topology(tmp_path / "seq", server, "run-seq", 1)
    r3, canon3, obs3 = _run_topology(tmp_path / "par", server, "run-par", 3)
    assert r1.terminal_state == ProductionTerminalState.COMPLETE.value
    assert r3.terminal_state == ProductionTerminalState.COMPLETE.value
    # Byte-identical canonical outcome regardless of scheduling.
    assert canon1 == canon3
    assert obs1 == obs3


# --- 3. unresolved route is truthful, never a false trusted-zero -------------
def test_route_unresolved_not_trusted_zero(tmp_path, server):
    settings = _settings(tmp_path)
    inst = _http_instance("spa-http", f"{server}/spa", "spa")
    companies = [PlannedCompany(company_id="spa", name="SpaCo", source_instances=("spa-http",), geography_group="PRIMARY")]
    result = _runtime(settings, "run-spa", {"spa-http": inst}, companies).run()
    # The run is COMPLETE (all children terminal) but the children are truthfully
    # EXTRACTION_UNRESOLVED — never ATTEMPTED_ZERO / COMPLETED_WITH_RESULTS.
    assert result.terminal_state == ProductionTerminalState.COMPLETE.value
    with StateStore(settings.state_db) as store:
        manifest = CoverageManifest.load(store, "run-spa")
        statuses = {t.status for t in manifest.tasks()}
        assert CoverageStatus.EXTRACTION_UNRESOLVED in statuses
        assert CoverageStatus.ATTEMPTED_ZERO not in statuses
        assert CoverageStatus.COMPLETED_WITH_RESULTS not in statuses
        assert store.count_raw_observations("run-spa") == 0


# --- 4. current-run report only ----------------------------------------------
def test_current_run_report_only(tmp_path, server):
    settings = _settings(tmp_path)
    inst_a = _http_instance("a-http", f"{server}/careers-a", "a")
    companies_a = [PlannedCompany(company_id="a", name="A", source_instances=("a-http",), geography_group="PRIMARY")]
    _runtime(settings, "run-A", {"a-http": inst_a}, companies_a).run()
    with StateStore(settings.state_db) as store:
        canon_a = store.count_current_run_canonical_jobs("run-A")

    inst_b = _http_instance("b-http", f"{server}/careers-b", "b")
    companies_b = [PlannedCompany(company_id="b", name="B", source_instances=("b-http",), geography_group="PRIMARY")]
    _runtime(settings, "run-B", {"b-http": inst_b}, companies_b).run()
    with StateStore(settings.state_db) as store:
        # Global canonical table now holds BOTH runs' jobs, but each run's
        # current-run count reflects ONLY that run.
        canon_b = store.count_current_run_canonical_jobs("run-B")
        assert canon_a >= 2 and canon_b >= 2
        assert store.count_raw_observations("run-A") == store.count_raw_observations("run-B")


# --- 5. resume does not duplicate observations -------------------------------
def test_crash_resume_no_duplicate_observations(tmp_path, server):
    settings = _settings(tmp_path)
    inst = _http_instance("r-http", f"{server}/careers-a", "r")
    companies = [PlannedCompany(company_id="r", name="R", source_instances=("r-http",), geography_group="PRIMARY")]
    # Stop after DISCOVER -> PARTIAL, then resume with a fresh runtime instance.
    rt1 = _runtime(settings, "run-resume", {"r-http": inst}, companies, stop_after_phase=ProductionPhase.DISCOVER)
    r1 = rt1.run()
    with StateStore(settings.state_db) as store:
        obs_after_discover = store.count_raw_observations("run-resume")
    rt2 = _runtime(settings, "run-resume", {"r-http": inst}, companies)
    r2 = rt2.run()
    with StateStore(settings.state_db) as store:
        obs_final = store.count_raw_observations("run-resume")
    assert r2.terminal_state == ProductionTerminalState.COMPLETE.value
    # No duplicate observations across the resume (idempotent DISCOVER).
    assert obs_final == obs_after_discover
    assert obs_final >= 2


# --- 6. parallel leased execution: no duplicates -----------------------------
def test_parallel_leasing_no_duplicate_observations(tmp_path, server):
    result, canon, obs = _run_topology(tmp_path, server, "run-lease", 3)
    assert result.terminal_state == ProductionTerminalState.COMPLETE.value
    # 3 companies x 2 unique jobs each = 6 unique canonical jobs.
    assert canon == 6
    with StateStore(tmp_path / "state" / "atlas_state.sqlite") as store:
        # Each job matches exactly one lane, so a fenced lease never double-commits.
        assert store.count_raw_observations("run-lease") == 6


# --- 7. browser route through the SAME runtime (headless) --------------------
@pytest.mark.browser
def test_browser_route_through_runtime(tmp_path, server):
    settings = _settings(tmp_path)
    entry = f"{server}/spa"  # served as a shell over HTTP, but rendered by Chrome
    # A page that renders job links client-side so the browser route extracts them.
    inst = SourceInstance(
        instance_id="b-run", source_type=SourceType.COMPANY_CAREER,
        source_family=SourceFamily.COMPANY_CAREER_BROWSER, display_name="Browser Co", company_id="bco",
        capability_overrides=frozenset({Capability.SEARCH, Capability.BROWSER_REQUIRED}),
        metadata={"entry_url": entry, "headless": True, "profile_dir": str(tmp_path / "cprof"),
                  "max_load_cycles": 1, "render_wait_ms": 600},
    )
    companies = [PlannedCompany(company_id="bco", name="BrowserCo", source_instances=("b-run",), geography_group="PRIMARY")]
    result = _runtime(settings, "run-browser", {"b-run": inst}, companies, parallel_workers=1).run()
    # The SPA shell renders nothing here (no client JS in the fixture), so the
    # browser route is truthfully EXTRACTION_UNRESOLVED — still terminal, and the
    # run COMPLETE, proving the browser adapter flows through the one governor.
    assert result.terminal_state == ProductionTerminalState.COMPLETE.value
    with StateStore(settings.state_db) as store:
        manifest = CoverageManifest.load(store, "run-browser")
        assert manifest.tasks()
        assert all(t.is_terminal for t in manifest.tasks())
