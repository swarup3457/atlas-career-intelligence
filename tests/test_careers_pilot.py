"""Phase 1C-B pilot tests: config sealing + an end-to-end pilot over LOCAL pages.

Offline: the pilot runs the COMPLETE production path (discovery -> routing ->
leased execution -> observations -> canonicalization -> verification -> one
current-run Excel report) against a local fixture server (127.0.0.1). One HTTP
company + one browser company prove both generic routes end to end and a PASS.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from atlas.careers.pilot import CareerPilot, PilotCompany, PilotConfig, load_pilot_config
from atlas.config import load_settings

pytestmark = pytest.mark.integration


_HTTP_JOBS = """<html><head><title>Careers</title>
<script type="application/ld+json">[
 {"@type":"JobPosting","title":"Java Backend Engineer","datePosted":"2026-08-01",
  "url":"http://HOST/jobs/1","identifier":"1",
  "jobLocation":{"address":{"addressLocality":"Bengaluru","addressCountry":"IN"}}},
 {"@type":"JobPosting","title":"Senior Software Engineer","datePosted":"2026-08-01",
  "url":"http://HOST/jobs/2","identifier":"2",
  "jobLocation":{"address":{"addressLocality":"Hyderabad","addressCountry":"IN"}}}
]</script></head><body>Real careers content about engineering.</body></html>"""

_BROWSER_JOBS = """<html><body><div id="root"></div>
<script>setTimeout(function(){document.getElementById('root').innerHTML=
'<a href="/jobs/10">Java Backend Engineer</a><a href="/jobs/11">Senior Software Engineer</a>';}, 120);</script>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        return

    def _send(self, body):
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        host = f"127.0.0.1:{self.server.server_address[1]}"
        path = self.path.split("?")[0]
        if path == "/http-careers":
            return self._send(_HTTP_JOBS.replace("HOST", host))
        if path == "/browser-careers":
            return self._send(_BROWSER_JOBS)
        return self._send("<html><body>detail</body></html>")


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
        output_dir=tmp_path / "output", logs_dir=tmp_path / "logs",
        browser_profile=tmp_path / "profile", agents_dir=tmp_path / "agents", skills_dir=tmp_path / "skills",
    )
    s.ensure_directories()
    return s


def test_config_load_and_seal_deterministic(tmp_path):
    cfg_text = """
run_id: t-pilot
lanes: [JAVA_BACKEND, GENERAL_SOFTWARE]
companies:
  - {company_id: a, name: A, official_domain: a.com, known_careers_url: 'https://a.com/careers'}
  - {company_id: b, name: B, official_domain: b.com, known_careers_url: 'https://b.com/careers'}
"""
    p = tmp_path / "pilot.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    c1 = load_pilot_config(p)
    c2 = load_pilot_config(p)
    assert c1.seal_hash() == c2.seal_hash()
    assert len(c1.companies) == 2
    # A different config yields a different hash.
    c3 = PilotConfig(run_id="t-pilot", companies=list(c1.companies)[:1])
    assert c3.seal_hash() != c1.seal_hash()


def test_pilot_rejects_more_than_12_companies():
    companies = [PilotCompany(company_id=str(i), name=str(i), official_domain=f"{i}.com") for i in range(13)]
    with pytest.raises(ValueError):
        PilotConfig(run_id="x", companies=companies)


def test_pilot_dry_run_is_offline(tmp_path, server):
    settings = _settings(tmp_path)
    config = PilotConfig(
        run_id="dry", companies=[
            PilotCompany(company_id="h", name="H", official_domain="127.0.0.1",
                         known_careers_url=f"{server}/http-careers"),
        ],
    )
    pilot = CareerPilot(settings, config)
    result = pilot.run(live=False)
    assert result.status == "DRY_RUN"
    assert result.runtime_terminal is None  # nothing executed


@pytest.mark.browser
def test_pilot_end_to_end_local_pass(tmp_path, server):
    settings = _settings(tmp_path)
    config = PilotConfig(
        run_id="e2e",
        companies=[
            PilotCompany(company_id="httpco", name="HttpCo", official_domain="127.0.0.1",
                         known_careers_url=f"{server}/http-careers", expected_route="GENERIC_HTTP"),
            PilotCompany(company_id="browserco", name="BrowserCo", official_domain="127.0.0.1",
                         known_careers_url=f"{server}/browser-careers", expected_route="GENERIC_BROWSER"),
        ],
        max_pages=1, max_jobs_per_company=20,
    )
    pilot = CareerPilot(settings, config, browser_profile_dir=tmp_path / "cprofile")
    result = pilot.run(live=True)
    assert result.runtime_terminal == "COMPLETE"
    assert result.report_valid is True
    # Both generic routes extracted real jobs; at least one HTTP + one browser.
    assert result.summary["http_success"] >= 1
    assert result.summary["browser_success"] >= 1
    assert result.status == "PASS"
    # Every sealed pilot company reached a terminal status.
    assert all(c.terminal_status not in ("NOT_ATTEMPTED", "IN_PROGRESS") for c in result.companies)
    # The report contains current-run pilot records (Bengaluru/Hyderabad software).
    assert result.summary["reported_jobs"] >= 1
