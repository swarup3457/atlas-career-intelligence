"""Phase 1C-A — the four official ATS adapters satisfy the shared adapter
contract harness (matrix C), driven by an injected FakeTransport (no network)."""

from __future__ import annotations

import pytest

from atlas.models import ErrorCategory
from atlas.sources.ats.ashby import AshbyAdapter
from atlas.sources.ats.greenhouse import GreenhouseAdapter
from atlas.sources.ats.lever import LeverAdapter
from atlas.sources.ats.workday import WorkdayAdapter
from atlas.sources.http_client import HttpError, HttpResponse
from atlas.sources.models import SourceInstance, SourceType
from atlas.sources.testing.contract import run_contract_checks
from atlas.sources.testing.http import FakeTransport, json_response

pytestmark = pytest.mark.unit


def _inst(iid, st, **md):
    return SourceInstance(instance_id=iid, source_type=st, display_name="Acme", metadata=md)


def _err_or(scenario, ok):
    def h(req):
        if scenario == "error:TIMEOUT":
            raise HttpError(ErrorCategory.TIMEOUT, "timeout")
        if scenario == "error:HTTP_429":
            return HttpResponse.build(429, headers={"Retry-After": "30"})
        if scenario == "error:HTTP_5XX":
            return HttpResponse.build(500)
        if scenario == "notfound":
            return HttpResponse.build(404)
        return ok(req)
    return h


_GH = {"id": 101, "internal_job_id": 5, "title": "Java Engineer",
       "updated_at": "2026-09-01T10:00:00-05:00", "location": {"name": "Bengaluru, India"},
       "absolute_url": "https://boards.greenhouse.io/acme/jobs/101", "requisition_id": "R1"}


def gh_factory(scenario):
    if scenario == "closed":
        return None

    def ok(req):
        if scenario == "malformed":
            return json_response({"jobs": [_GH, {"nope": 1}, dict(_GH, id=102)], "meta": {"total": 3}})
        if scenario in ("zero", "untrusted_zero", "selector_drift"):
            return json_response({"jobs": [], "meta": {"total": 0}})
        if scenario == "unicode":
            return json_response({"jobs": [dict(_GH, id=201, title="Se\u00f1or Caf\u00e9 \u8f6f\u4ef6 Engineer")], "meta": {"total": 1}})
        return json_response({"jobs": [_GH, dict(_GH, id=102, title="Java II")], "meta": {"total": 2}})
    return GreenhouseAdapter(_inst("gh", SourceType.ATS_GREENHOUSE, board_token="acme"),
                             http_client=FakeTransport(_err_or(scenario, ok)))


_LV = {"id": "u1", "text": "Java Engineer",
       "categories": {"location": "Bengaluru", "department": "Eng", "team": "Plat", "commitment": "Full-time"},
       "workplaceType": "remote", "hostedUrl": "https://jobs.lever.co/acme/u1", "applyUrl": "x",
       "createdAt": 1693561200000, "descriptionPlain": "desc"}


def lv_factory(scenario):
    if scenario == "closed":
        return None

    def ok(req):
        if scenario == "malformed":
            return json_response([_LV, {"nope": 1}, dict(_LV, id="u2")])
        if scenario in ("zero", "untrusted_zero", "selector_drift"):
            return json_response([])
        if scenario == "unicode":
            return json_response([dict(_LV, id="u9", text="Se\u00f1or Caf\u00e9 \u8f6f\u4ef6")])
        return json_response([_LV, dict(_LV, id="u2", text="Java II")])
    return LeverAdapter(_inst("lv", SourceType.ATS_LEVER, site="acme"),
                        http_client=FakeTransport(_err_or(scenario, ok)))


_AS = {"title": "Java Engineer", "location": "Bengaluru", "department": "Eng", "isListed": True,
       "isRemote": True, "workplaceType": "Remote", "descriptionPlain": "desc",
       "publishedAt": "2026-08-15T10:00:00.000+00:00", "employmentType": "FullTime",
       "jobUrl": "https://jobs.ashbyhq.com/acme/u1", "applyUrl": "x"}


def ash_factory(scenario):
    if scenario in ("closed", "notfound"):  # Ashby has no detail endpoint
        return None

    def ok(req):
        if scenario == "malformed":
            return json_response({"apiVersion": "1", "jobs": [_AS, {"nope": 1}, dict(_AS, jobUrl="https://jobs.ashbyhq.com/acme/u2")]})
        if scenario in ("zero", "untrusted_zero", "selector_drift"):
            return json_response({"apiVersion": "1", "jobs": []})
        if scenario == "unicode":
            return json_response({"apiVersion": "1", "jobs": [dict(_AS, title="Se\u00f1or Caf\u00e9 \u8f6f\u4ef6", jobUrl="https://jobs.ashbyhq.com/acme/u9")]})
        return json_response({"apiVersion": "1", "jobs": [_AS, dict(_AS, jobUrl="https://jobs.ashbyhq.com/acme/u2", title="Java II")]})
    return AshbyAdapter(_inst("as", SourceType.ATS_ASHBY, board_name="acme"),
                        http_client=FakeTransport(_err_or(scenario, ok)))


_WD = {"title": "Java Engineer", "externalPath": "/job/Bengaluru/Java_R1", "locationsText": "Bengaluru, India",
       "postedOn": "Posted 5 Days Ago", "bulletFields": ["R1"], "jobReqId": "R1"}
_WDD = {"jobPostingInfo": {"title": "Java Engineer", "jobDescription": "<p>d</p>", "location": "Bengaluru",
                           "jobReqId": "R1", "externalPath": "/job/Bengaluru/Java_R1", "postedOn": "Posted 5 Days Ago"}}


def wd_factory(scenario):
    if scenario == "closed":
        return None

    def ok(req):
        if req.method == "GET":  # detail
            return json_response(_WDD)
        if scenario == "malformed":
            return json_response({"total": 3, "jobPostings": [_WD, {"nope": 1}, dict(_WD, jobReqId="R2")]})
        if scenario in ("zero", "untrusted_zero", "selector_drift"):
            return json_response({"total": 0, "jobPostings": []})
        if scenario == "unicode":
            return json_response({"total": 1, "jobPostings": [dict(_WD, title="Se\u00f1or Caf\u00e9 \u8f6f\u4ef6", jobReqId="R9")]})
        return json_response({"total": 2, "jobPostings": [_WD, dict(_WD, jobReqId="R2")]})
    return WorkdayAdapter(_inst("wd", SourceType.ATS_WORKDAY, tenant="acme", datacenter="wd1", site="External"),
                          http_client=FakeTransport(_err_or(scenario, ok)))


@pytest.mark.parametrize("factory,name", [
    (gh_factory, "greenhouse"), (lv_factory, "lever"), (ash_factory, "ashby"), (wd_factory, "workday"),
])
def test_official_ats_adapter_passes_contract(factory, name):
    report = run_contract_checks(factory)
    assert report.ok, f"{name}:\n" + report.render()


def test_union_covers_core_categories():
    reports = [run_contract_checks(f) for f in (gh_factory, lv_factory, ash_factory, wd_factory)]
    core = {"registration_identity", "typed_result_schema", "one_bad_item_isolation",
            "map_429", "map_5xx", "map_timeout", "zero_result_trusted", "zero_result_untrusted",
            "sentinel_behavior", "selector_drift", "unicode", "health_check", "no_secret_evidence"}
    passed = {n for n in core if any(r.passed(n) for r in reports)}
    assert core - passed == set(), f"never passed by any adapter: {sorted(core - passed)}"
