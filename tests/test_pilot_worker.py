"""Failing-first tests for the LLM company-search worker + constrained tools (audit 3.1, s.4/5).

Offline + deterministic: a fake read-only HTTP client returns canned official evidence so the
worker drives the SAME six tools Python-side (the deterministic equivalent of the live LLM
tool loop), with zero network.
"""

from __future__ import annotations

import json

import pytest

from atlas.pilot.config import load_pilot_config
from atlas.pilot.models import CompanyStatus
from atlas.pilot.tools import CompanySearchToolbox
from atlas.pilot.usage import UsageMeter
from atlas.pilot.worker import CompanyTask, LlmCompanySearchWorker, build_company_prompt
from atlas.sources.http_client import HttpResponse

_CAREERS_HTML = (
    "<!doctype html><html><body>Careers at TestCo. Apply via our board: "
    "https://boards.greenhouse.io/testco see openings.</body></html>"
)

_BOARD = {
    "jobs": [
        {"id": 101, "title": "Java Backend Engineer", "location": {"name": "Bengaluru, India"},
         "absolute_url": "https://boards.greenhouse.io/testco/jobs/101", "requisition_id": "RB1",
         "content": "Java Spring Boot REST APIs microservices. 2-3 years."},
        {"id": 102, "title": "Staff Backend Engineer (Go)", "location": {"name": "Bengaluru, India"},
         "absolute_url": "https://boards.greenhouse.io/testco/jobs/102", "requisition_id": "RB2",
         "content": "Golang microservices. 8+ years."},
        {"id": 103, "title": "Senior Java Engineer", "location": {"name": "San Francisco, CA"},
         "absolute_url": "https://boards.greenhouse.io/testco/jobs/103", "requisition_id": "RB3",
         "content": "Java Spring Boot. 3 years."},
        {"id": 104, "title": "React Developer", "location": {"name": "Hyderabad, India"},
         "absolute_url": "https://boards.greenhouse.io/testco/jobs/104", "requisition_id": "RB4",
         "content": "React ReactJS TypeScript responsive UI. 2 years."},
    ]
}

_DETAILS = {
    "101": {"id": 101, "title": "Java Backend Engineer", "location": {"name": "Bengaluru, India"},
            "absolute_url": "https://boards.greenhouse.io/testco/jobs/101", "requisition_id": "RB1",
            "first_published": "2026-09-01", "updated_at": "2026-09-05",
            "content": "<p>Java Spring Boot REST APIs, Hibernate, MySQL, microservices. 2-3 years experience.</p>"},
    "104": {"id": 104, "title": "React Developer", "location": {"name": "Hyderabad, India"},
            "absolute_url": "https://boards.greenhouse.io/testco/jobs/104", "requisition_id": "RB4",
            "first_published": "2026-09-02", "updated_at": "2026-09-06",
            "content": "<p>React, ReactJS, TypeScript, responsive UI, Redux. 2 years.</p>"},
}


class FakeHttpClient:
    def __init__(self):
        self.requests = []

    def fetch(self, request):
        url = request.url
        self.requests.append(url)
        if url.endswith("/careers") or "careers" in url:
            return HttpResponse.build(200, body=_CAREERS_HTML.encode(),
                                      headers={"Content-Type": "text/html"}, url=url)
        if "/v1/boards/" in url and url.rstrip("?content=true").endswith("/jobs"):
            return HttpResponse.build(200, body=json.dumps(_BOARD).encode(),
                                      headers={"Content-Type": "application/json"}, url=url)
        if "content=true" in url and "/jobs" in url:
            return HttpResponse.build(200, body=json.dumps(_BOARD).encode(),
                                      headers={"Content-Type": "application/json"}, url=url)
        import re
        m = re.search(r"/jobs/(\d+)$", url)
        if m and m.group(1) in _DETAILS:
            return HttpResponse.build(200, body=json.dumps(_DETAILS[m.group(1)]).encode(),
                                      headers={"Content-Type": "application/json"}, url=url)
        return HttpResponse.build(404, body=b"{}", headers={"Content-Type": "application/json"}, url=url)


@pytest.fixture(scope="module")
def config():
    return load_pilot_config()


def _worker(config):
    return LlmCompanySearchWorker(config=config, mode="deterministic",
                                  profile_summary={"target_lanes": list(config.primary_lanes),
                                                   "experience_years": 2.0,
                                                   "preferred_locations": ["Bengaluru", "Hyderabad"]})


def _toolbox_with_fake(config, company="TestCo"):
    tb = CompanySearchToolbox(company=company, config=config, task_id="T1", client=FakeHttpClient())
    return tb


def test_unmapped_company_invokes_official_discovery(config):
    tb = _toolbox_with_fake(config)
    tb.resolve_official_company_site("TestCo", ["testco.example"])
    out = tb.discover_official_careers_entry(domain="testco.example")
    assert out["ats_resolved"] is True
    assert tb.discovery.ats[0] == "greenhouse"


def test_company_agent_must_use_tools_not_memory(config):
    tb = _toolbox_with_fake(config)
    w = _worker(config)
    # drive deterministically over the fake client
    tb2 = CompanySearchToolbox(company="TestCo", config=config, task_id="T1", client=FakeHttpClient())
    w._run_deterministic(CompanyTask(company="TestCo", task_id="T1", domain_hint="testco.example"), tb2)
    res = tb2.submitted or tb2.build_result()
    assert res.tool_calls >= 5  # resolve + discover + searches + submit
    assert res.evidence_urls, "a real search must surface official evidence URLs"


def test_official_domain_validation_rejects_foreign_urls(config):
    tb = _toolbox_with_fake(config)
    tb.discovery = None
    assert tb._is_official_url("http://evil.example/jobs/1") is False
    assert tb._is_official_url("https://boards.greenhouse.io/testco/jobs/1") is True
    assert tb._is_official_url("https://phish.example/careers") is False


def test_search_checklist_completion_over_fake_board(config):
    tb = CompanySearchToolbox(company="TestCo", config=config, task_id="T1", client=FakeHttpClient())
    w = _worker(config)
    w._run_deterministic(CompanyTask(company="TestCo", task_id="T1", domain_hint="testco.example"), tb)
    res = tb.submitted or tb.build_result()
    assert res.lane_checklist_complete(config.primary_lanes) is True
    assert res.status in (CompanyStatus.COMPLETE.value, CompanyStatus.COMPLETE_NO_MATCHES.value)
    # India job details were opened; the Go/foreign jobs are surfaced as cards but the
    # deterministic worker collects details for candidate India roles.
    assert any("Bengaluru" in (j.location or "") or "Hyderabad" in (j.location or "") for j in res.jobs)


def test_unresolved_company_truthful_status(config):
    tb = CompanySearchToolbox(company="NoSuchCo", config=config, task_id="T1", client=FakeHttpClient())
    w = _worker(config)
    w._run_deterministic(CompanyTask(company="NoSuchCo", task_id="T1"), tb)
    res = tb.submitted or tb.build_result()
    assert res.status == CompanyStatus.OFFICIAL_SOURCE_UNRESOLVED.value


def test_build_sdk_tools_exposes_six_tools(config):
    tb = _toolbox_with_fake(config)
    from atlas.pilot.tools import build_sdk_tools

    tools = build_sdk_tools(tb)
    assert len(tools) == 6
    names = {t.name for t in tools}
    assert "submit_company_search_result" in names
    assert "search_official_career_site" in names


def test_company_prompt_is_grounded(config):
    prompt = build_company_prompt(
        CompanyTask(company="Fiserv", task_id="T1", domain_hint="fiserv.com"),
        config, {"target_lanes": list(config.primary_lanes), "experience_years": 2.0,
                 "preferred_locations": ["Bengaluru"]},
    )
    assert "Fiserv" in prompt
    assert "submit_company_search_result" in prompt
    assert "never answer from memory" in prompt.lower()
