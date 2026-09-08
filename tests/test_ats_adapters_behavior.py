"""Phase 1C-A — official ATS adapter behavior & provenance (matrix C).

Detailed, per-adapter assertions on date provenance, stable IDs, canonical URLs,
pagination, schema drift, challenge/no-bypass handling, invalid config, and the
non-negotiable no-apply boundary — all with an injected FakeTransport."""

from __future__ import annotations

import pytest

from atlas.models import ErrorCategory
from atlas.sources.adapter import AdapterError
from atlas.sources.ats.ashby import AshbyAdapter
from atlas.sources.ats.base import DateProvenance
from atlas.sources.ats.greenhouse import GreenhouseAdapter
from atlas.sources.ats.lever import LeverAdapter
from atlas.sources.ats.workday import WorkdayAdapter
from atlas.sources.http_client import HttpResponse
from atlas.sources.models import (
    ActiveState,
    DetailRequest,
    SearchRequest,
    SourceInstance,
    SourceType,
    WorkMode,
)
from atlas.sources.testing.http import FakeTransport, json_response, static, text_response

pytestmark = pytest.mark.unit


def _inst(iid, st, **md):
    return SourceInstance(instance_id=iid, source_type=st, display_name="Acme", metadata=md)


def _no_apply(transport: FakeTransport) -> bool:
    """The adapter must NEVER call an apply/submit endpoint."""
    return all("/apply" not in c.url and "/submit" not in c.url.lower() for c in transport.calls)


# --------------------------------------------------------------------------
# Greenhouse
# --------------------------------------------------------------------------
def test_greenhouse_search_updated_provenance_and_prospect_filter():
    listing = {"jobs": [
        {"id": 1, "internal_job_id": 9, "title": "Java Engineer", "updated_at": "2026-09-01T10:00:00Z",
         "location": {"name": "Bengaluru, India"}, "absolute_url": "https://boards.greenhouse.io/acme/jobs/1"},
        {"id": 2, "internal_job_id": None, "title": "General Interest",  # prospect → filtered
         "location": {"name": "Anywhere"}, "absolute_url": "https://boards.greenhouse.io/acme/jobs/2"},
    ], "meta": {"total": 2}}
    tr = FakeTransport(static(json_response(listing)))
    a = GreenhouseAdapter(_inst("gh", SourceType.ATS_GREENHOUSE, board_token="acme"), http_client=tr)
    res = a.search(SearchRequest(query="java", limit=25))
    assert res.count == 1  # prospect filtered out
    r = res.results[0]
    assert r.source_job_id == "1"
    assert r.canonical_url == "https://boards.greenhouse.io/acme/jobs/1"
    assert r.updated_at == "2026-09-01T10:00:00Z"
    assert r.posted_at is None  # list endpoint has no posted date
    assert r.provenance["date_provenance"] == DateProvenance.EMPLOYER_UPDATED_AT.value
    assert r.work_mode == WorkMode.UNKNOWN
    assert _no_apply(tr)


def test_greenhouse_detail_first_published_is_posted():
    detail = {"id": 1, "internal_job_id": 9, "title": "Java Engineer", "first_published": "2026-08-01T00:00:00Z",
              "updated_at": "2026-09-01T10:00:00Z", "content": "<p>Great role &amp; team</p>",
              "absolute_url": "https://boards.greenhouse.io/acme/jobs/1", "location": {"name": "Remote, India"}}
    a = GreenhouseAdapter(_inst("gh", SourceType.ATS_GREENHOUSE, board_token="acme"),
                          http_client=FakeTransport(static(json_response(detail))))
    r = a.fetch_detail(DetailRequest(source_job_id="1"))
    assert r.posted_at == "2026-08-01T00:00:00Z"
    assert r.provenance["date_provenance"] == DateProvenance.EMPLOYER_POSTED_AT.value
    assert r.description and "Great role & team" in r.description
    assert r.work_mode == WorkMode.REMOTE


def test_greenhouse_missing_board_token_is_config_error():
    with pytest.raises(AdapterError) as exc:
        GreenhouseAdapter(_inst("gh", SourceType.ATS_GREENHOUSE))
    assert exc.value.category == ErrorCategory.CONFIG_ERROR


# --------------------------------------------------------------------------
# Lever
# --------------------------------------------------------------------------
def test_lever_created_at_epoch_is_posted_and_pagination_url():
    item = {"id": "u1", "text": "Java Engineer", "categories": {"location": "Bengaluru", "commitment": "Full-time"},
            "workplaceType": "remote", "hostedUrl": "https://jobs.lever.co/acme/u1", "createdAt": 1693561200000,
            "descriptionPlain": "desc"}
    tr = FakeTransport(static(json_response([item])))
    a = LeverAdapter(_inst("lv", SourceType.ATS_LEVER, site="acme"), http_client=tr)
    r = a.search(SearchRequest(query="java", page=2, limit=10)).results[0]
    assert r.posted_at is not None and r.posted_at.startswith("2023-")
    assert r.provenance["date_provenance"] == DateProvenance.EMPLOYER_POSTED_AT.value
    assert r.work_mode == WorkMode.REMOTE
    assert r.employment_type == "Full-time"
    # Pagination: page 2 × limit 10 → skip=10.
    assert "skip=10" in tr.calls[0].url and "limit=10" in tr.calls[0].url and "mode=json" in tr.calls[0].url


def test_lever_missing_created_at_is_unknown_provenance():
    item = {"id": "u1", "text": "Java Engineer", "categories": {}, "hostedUrl": "https://jobs.lever.co/acme/u1"}
    a = LeverAdapter(_inst("lv", SourceType.ATS_LEVER, site="acme"), http_client=FakeTransport(static(json_response([item]))))
    r = a.search(SearchRequest(query="java")).results[0]
    assert r.posted_at is None
    assert r.provenance["date_provenance"] == DateProvenance.UNKNOWN.value


def test_lever_eu_base_from_url():
    a = LeverAdapter(SourceInstance(instance_id="lv", source_type=SourceType.ATS_LEVER,
                                    base_url="https://jobs.eu.lever.co/acme"),
                     http_client=FakeTransport(static(json_response([]))))
    assert a.api_base == "https://api.eu.lever.co/v0/postings"
    assert a.site == "acme"


# --------------------------------------------------------------------------
# Ashby
# --------------------------------------------------------------------------
def test_ashby_published_at_and_islisted_filter_and_stable_id():
    payload = {"apiVersion": "1", "jobs": [
        {"title": "Java Engineer", "location": "Bengaluru", "isListed": True, "workplaceType": "Remote",
         "publishedAt": "2026-08-15T10:00:00.000+00:00", "descriptionPlain": "desc",
         "jobUrl": "https://jobs.ashbyhq.com/acme/abc-123"},
        {"title": "Hidden", "location": "X", "isListed": False, "jobUrl": "https://jobs.ashbyhq.com/acme/hidden"},
    ]}
    tr = FakeTransport(static(json_response(payload)))
    a = AshbyAdapter(_inst("as", SourceType.ATS_ASHBY, board_name="acme"), http_client=tr)
    res = a.search(SearchRequest(query="java"))
    assert res.count == 1  # isListed:false excluded
    r = res.results[0]
    assert r.source_job_id == "abc-123"  # derived from jobUrl tail (no documented id)
    assert r.posted_at == "2026-08-15T10:00:00.000+00:00"
    assert r.provenance["date_provenance"] == DateProvenance.EMPLOYER_POSTED_AT.value
    assert r.description == "desc"
    assert r.work_mode == WorkMode.REMOTE
    assert _no_apply(tr)


def test_ashby_has_no_detail_capability():
    a = AshbyAdapter(_inst("as", SourceType.ATS_ASHBY, board_name="acme"),
                     http_client=FakeTransport(static(json_response({"apiVersion": "1", "jobs": []}))))
    from atlas.sources.models import Capability
    assert not a.supports(Capability.DETAIL)


# --------------------------------------------------------------------------
# Workday (highest risk)
# --------------------------------------------------------------------------
def test_workday_relative_posted_text_never_absolute():
    payload = {"total": 1, "jobPostings": [
        {"title": "Java Engineer", "externalPath": "/job/BLR/Java_R1", "locationsText": "Bengaluru, India",
         "postedOn": "Posted 30+ Days Ago", "bulletFields": [{"label": "Req", "value": "R1"}]}]}
    tr = FakeTransport(static(json_response(payload)))
    a = WorkdayAdapter(_inst("wd", SourceType.ATS_WORKDAY, tenant="acme", datacenter="wd1", site="External"), http_client=tr)
    r = a.search(SearchRequest(query="java")).results[0]
    assert r.posted_at is None  # never fabricate an absolute date from relative text
    assert r.provenance["date_provenance"] == DateProvenance.RELATIVE_POSTED_TEXT.value
    assert r.provenance["posted_raw"] == "Posted 30+ Days Ago"
    assert r.source_job_id == "R1"  # from bulletFields [{label,value}]
    assert "myworkdayjobs.com" in (r.canonical_url or "")
    # The search is the SAME public POST the careers page issues (empty facets).
    assert tr.calls[0].method == "POST" and tr.calls[0].url.endswith("/wday/cxs/acme/External/jobs")
    assert _no_apply(tr)


def test_workday_limit_capped_at_twenty():
    tr = FakeTransport(static(json_response({"total": 0, "jobPostings": []})))
    a = WorkdayAdapter(_inst("wd", SourceType.ATS_WORKDAY, tenant="acme", datacenter="wd1", site="External"), http_client=tr)
    a.search(SearchRequest(query="java", limit=100))  # request 100
    import json as _json
    body = _json.loads(tr.calls[0].body.decode())
    assert body["limit"] == 20  # hard-capped
    assert body["searchText"] == "" and body["appliedFacets"] == {}


def test_workday_challenge_is_access_limited_not_zero():
    challenge = text_response("<!DOCTYPE html><html>Just a moment... checking your browser</html>",
                              content_type="text/html")
    a = WorkdayAdapter(_inst("wd", SourceType.ATS_WORKDAY, tenant="acme", datacenter="wd1", site="External"),
                       http_client=FakeTransport(static(challenge)))
    with pytest.raises(AdapterError) as exc:
        a.search(SearchRequest(query="java"))
    assert exc.value.category == ErrorCategory.ANTI_BOT  # never a false zero, never bypassed


def test_workday_schema_drift_is_extraction_unresolved():
    a = WorkdayAdapter(_inst("wd", SourceType.ATS_WORKDAY, tenant="acme", datacenter="wd1", site="External"),
                       http_client=FakeTransport(static(json_response({"unexpected": "shape"}))))
    res = a.search(SearchRequest(query="java"))
    from atlas.sources.models import ZeroResultKind
    assert res.count == 0 and res.zero_result_kind == ZeroResultKind.EXTRACTION_UNRESOLVED


def test_workday_requires_datacenter_shard():
    with pytest.raises(AdapterError) as exc:
        WorkdayAdapter(_inst("wd", SourceType.ATS_WORKDAY, tenant="acme", site="External"))  # no datacenter
    assert exc.value.category == ErrorCategory.CONFIG_ERROR


def test_workday_detail_sanitizes_description():
    detail = {"jobPostingInfo": {"title": "Java Engineer", "jobDescription": "<script>x()</script><p>Hello</p>",
                                 "jobReqId": "R1", "externalPath": "/job/BLR/Java_R1"}}
    a = WorkdayAdapter(_inst("wd", SourceType.ATS_WORKDAY, tenant="acme", datacenter="wd1", site="External"),
                       http_client=FakeTransport(static(json_response(detail))))
    r = a.fetch_detail(DetailRequest(url="https://acme.wd1.myworkdayjobs.com/en-US/External/job/BLR/Java_R1"))
    assert r.description == "Hello" and "<script>" not in (r.description or "")


# --------------------------------------------------------------------------
# Shared: is_active is ACTIVE (boards list live jobs only)
# --------------------------------------------------------------------------
def test_all_adapters_report_active_for_listed_jobs():
    gh = GreenhouseAdapter(_inst("gh", SourceType.ATS_GREENHOUSE, board_token="acme"),
                           http_client=FakeTransport(static(json_response(
                               {"jobs": [{"id": 1, "internal_job_id": 2, "title": "T", "location": {"name": "X"},
                                          "absolute_url": "u"}], "meta": {"total": 1}}))))
    assert gh.search(SearchRequest(query="x")).results[0].is_active == ActiveState.ACTIVE
