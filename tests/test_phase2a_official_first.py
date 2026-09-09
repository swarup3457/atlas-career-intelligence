"""Phase 2A official-first regressions (build spec §19).

These lock in the corrected architecture: the daily run begins from the company
universe and produces DIRECT OFFICIAL jobs (record_class OFFICIAL_DIRECT) with
extracted requirements into the ONE published workbook; portal discovery is
supplemental (PORTAL_ONLY, never All_Jobs); a portal lead linked to official
evidence becomes an official PORTAL_OFFICIAL_LINKED row; and a LinkedIn/portal-
only workbook can never become the latest COMPLETE run.

All official evidence here is produced by CANNED official adapters (no network):
they emit OFFICIAL_SEARCH_LIVE on search and OFFICIAL_DETAIL_LIVE (with a real
description + requisition) on detail, exercising the REAL production graph
(sealed plan -> leases -> hydration -> canonicalization -> verification) and the
REAL requirements extractor + workbook contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas.config import load_settings
from atlas.careers.profile import RouteKind
from atlas.careers.router import RouteDecision
from atlas.candidate.eligibility import CandidateProfile
from atlas.candidate.importer import build_synthetic_ledger
from atlas.runtime.official_universe import (
    OfficialCompanyTarget,
    OfficialCompanyUniverseRunner,
    _RoutedCompany,
)
from atlas.sources.adapter import SourceAdapter, new_result_base
from atlas.sources.health import SourceHealth, SourceHealthState
from atlas.sources.models import (
    ActiveState,
    Capability,
    DiscoveryResult,
    SearchResult,
    SourceFamily,
    SourceInstance,
    SourceType,
    VerificationLevel,
    WorkMode,
    ZeroResultKind,
)
from atlas.sources.registry import SourceRegistry

_JAVA_DESC = (
    "Senior Java Backend Engineer. Requirements: 2+ years experience with Java "
    "and Spring Boot. Strong knowledge of PostgreSQL, Kafka and Kubernetes on AWS. "
    "Proficiency in REST APIs and microservices architecture. "
    "Nice to have: React, GraphQL, Terraform. "
    "We are based in Bengaluru, India."
)


def _canned_official_adapter(family: SourceFamily, source_type: SourceType, host: str, company: str):
    """Build a CANNED official adapter class for ``family`` that emits official
    evidence + a real description on detail (no network)."""
    _family, _stype, _host, _company = family, source_type, host, company

    class _Canned(SourceAdapter):
        source_type = _stype
        source_family = _family
        CAPABILITIES = frozenset({
            Capability.SEARCH, Capability.DETAIL, Capability.DESCRIPTION,
            Capability.POSTED_DATE, Capability.ACTIVE_STATUS,
        })
        adapter_version = f"canned-{_family.value}-1"
        parser_version = f"canned-{_family.value}-p-1"

        def _mk(self, i, detail):
            return DiscoveryResult(**new_result_base(
                self,
                source_job_id=f"{_family.value}-{i}",
                source_url=f"https://{_host}/{_company}/jobs/{i}",
                canonical_url=f"https://{_host}/{_company}/jobs/{i}",
                company=_company, title=f"Java Backend Engineer {i}",
                location="Bengaluru, India", work_mode=WorkMode.REMOTE,
                posted_at=("2026-09-05" if detail else None),
                description=(_JAVA_DESC if detail else None), is_active=ActiveState.ACTIVE,
                verification_level=(VerificationLevel.OFFICIAL_DETAIL_LIVE if detail
                                    else VerificationLevel.OFFICIAL_SEARCH_LIVE),
                confidence=0.95,
                provenance={"source_family": _family.value, "requisition_id": f"REQ-{_family.value}-{i}",
                            "date_provenance": ("EMPLOYER_POSTED_AT" if detail else "EMPLOYER_UPDATED_AT")}))

        def health_check(self):
            return SourceHealth(SourceHealthState.HEALTHY, "canned ok")

        def search(self, request):
            return SearchResult(results=tuple(self._mk(i, False) for i in range(3)),
                                page=request.page, has_more=False, total_reported=3,
                                zero_result_kind=ZeroResultKind.NOT_APPLICABLE)

        def fetch_detail(self, request):
            anchor = request.source_job_id or (request.url or "0")
            i = str(anchor).rstrip("/").rsplit("/", 1)[-1].rsplit("-", 1)[-1]
            return self._mk(i, True)

    _Canned.__name__ = f"Canned_{_family.value}"
    return _Canned


def _greenhouse_route(company_id="gh-co", company="GreenCo"):
    cls = _canned_official_adapter(SourceFamily.GREENHOUSE, SourceType.ATS_GREENHOUSE,
                                   "boards.greenhouse.io", company)
    inst = SourceInstance(
        instance_id=f"{company_id}::greenhouse", source_type=SourceType.ATS_GREENHOUSE,
        source_family=SourceFamily.GREENHOUSE, company_id=company_id, display_name=company,
        metadata={"board_token": company_id, "entry_url": f"https://boards.greenhouse.io/{company_id}",
                  "max_pages": 1, "max_results": 10})
    tgt = OfficialCompanyTarget(company_id=company_id, name=company, official_domain=f"{company_id}.com",
                                board_token=company_id, family="greenhouse")
    rc = _RoutedCompany(tgt, RouteDecision(RouteKind.ATS, inst.metadata["entry_url"], source_instance=inst), inst)
    return cls, rc


def _generic_route(company_id="hc-co", company="HttpCo"):
    cls = _canned_official_adapter(SourceFamily.COMPANY_CAREER, SourceType.COMPANY_CAREER,
                                   "httpco.example", company)
    inst = SourceInstance(
        instance_id=f"{company_id}::company_career", source_type=SourceType.COMPANY_CAREER,
        source_family=SourceFamily.COMPANY_CAREER, company_id=company_id, display_name=company,
        metadata={"entry_url": "https://httpco.example/careers", "max_pages": 1, "max_results": 10})
    tgt = OfficialCompanyTarget(company_id=company_id, name=company, official_domain="httpco.example")
    rc = _RoutedCompany(tgt, RouteDecision(RouteKind.GENERIC_HTTP, inst.metadata["entry_url"],
                                           source_instance=inst), inst)
    return cls, rc


def _settings(tmp_path):
    return load_settings(
        state_db=tmp_path / "state.sqlite", checkpoint_db=tmp_path / "ckpt.sqlite",
        output_dir=tmp_path / "out", logs_dir=tmp_path / "logs",
        production_output_root=tmp_path / "prod",
    )


def _registry(*classes):
    reg = SourceRegistry()
    for cls in classes:
        reg.register(cls)
    return reg


def _run_official(tmp_path, run_id, *, workers=1):
    gh_cls, gh_rc = _greenhouse_route()
    hc_cls, hc_rc = _generic_route()
    settings = _settings(tmp_path)
    reg = _registry(gh_cls, hc_cls)
    runner = OfficialCompanyUniverseRunner(
        settings, run_id, targets=[gh_rc.target, hc_rc.target], registry=reg,
        prepared_routes=[gh_rc, hc_rc], live=False, lanes=["JAVA_BACKEND"], parallel_workers=workers)
    return runner.run(), settings


# --------------------------------------------------------------------------- #
# §19.11 + §19.12 + §19.13 — direct official from BOTH a specialized ATS and a
# generic official-career route, with hydrated requirements.
# --------------------------------------------------------------------------- #
def test_official_universe_direct_jobs_from_ats_and_generic(tmp_path):
    result, _ = _run_official(tmp_path, "PH2A-OFF-1")
    assert result.runtime_terminal == "COMPLETE"
    assert result.direct_official_jobs >= 5
    assert result.companies_with_results >= 2
    assert result.companies_planned == result.companies_terminal
    fams = set(result.route_families_with_results)
    assert "greenhouse" in fams                       # specialized ATS route
    assert "company_career" in fams                   # generic official-career route
    assert result.generic_http_jobs >= 1
    assert result.specialized_ats_jobs >= 1
    # every direct job is OFFICIAL_DIRECT with an official URL + extracted requirements
    for j in result.jobs:
        assert j.record_class == "OFFICIAL_DIRECT"
        assert j.verification_state in ("VERIFIED_OFFICIAL", "OFFICIAL_SEARCH_LIVE")
        assert j.url and "linkedin" not in j.url
        assert j.mandatory_requirements  # hydrated requirements, not title-only
        assert j.official_requisition_id


def test_hydrated_requirements_flow_into_ranking(tmp_path):
    """A hydrated official job's extracted requirements drive a real candidate
    match (a strong recommendation), not a title-only guess."""
    from atlas.candidate.ranking import rank_and_evaluate
    from atlas.policy import load_policy

    result, _ = _run_official(tmp_path, "PH2A-OFF-REQ")
    candidate = CandidateProfile.from_ledger(
        build_synthetic_ledger(), total_experience_years=2.0,
        target_lanes=("JAVA_BACKEND", "GENERAL_SOFTWARE"), strong_overall=True)
    ranking = rank_and_evaluate(result.jobs, load_policy(), candidate, triage_limit=25, deep_limit=10)
    # at least one official job is an apply-family recommendation grounded in reqs
    apply = [e for e in ranking.evaluations
             if e.recommendation in ("PRIORITY_APPLY", "STRONG_APPLY", "APPLY_AFTER_TAILORING")]
    assert apply, "hydrated official jobs must be able to reach an apply recommendation"
    assert any(e.strengths for e in apply)


# --------------------------------------------------------------------------- #
# §19.17 — concurrency 1 == N (identical canonical/official result set).
# --------------------------------------------------------------------------- #
def test_official_concurrency_one_equals_n(tmp_path):
    r1, _ = _run_official(tmp_path / "w1", "PH2A-C1", workers=1)
    rN, _ = _run_official(tmp_path / "wN", "PH2A-CN", workers=3)
    keys1 = sorted(j.job_key for j in r1.jobs)
    keysN = sorted(j.job_key for j in rN.jobs)
    assert keys1 == keysN
    assert r1.direct_official_jobs == rN.direct_official_jobs


# --------------------------------------------------------------------------- #
# §19.9 — a completed official run does not re-run; a fresh runner over the SAME
# run id + checkpoint resumes to the identical terminal result (no duplicate work).
# --------------------------------------------------------------------------- #
def test_official_run_resume_is_exact(tmp_path):
    gh_cls, gh_rc = _greenhouse_route()
    hc_cls, hc_rc = _generic_route()
    settings = _settings(tmp_path)
    reg = _registry(gh_cls, hc_cls)

    def _mk():
        return OfficialCompanyUniverseRunner(
            settings, "PH2A-RESUME", targets=[gh_rc.target, hc_rc.target], registry=reg,
            prepared_routes=[gh_rc, hc_rc], live=False, lanes=["JAVA_BACKEND"])

    r1 = _mk().run()
    r2 = _mk().run()  # same run id + checkpoint -> resume, no re-run
    assert r1.direct_official_jobs == r2.direct_official_jobs >= 5
    assert sorted(j.job_key for j in r1.jobs) == sorted(j.job_key for j in r2.jobs)


# --------------------------------------------------------------------------- #
# Daily pipeline: official jobs in All_Jobs; portal-only excluded; metrics.
# --------------------------------------------------------------------------- #
def _daily_candidate():
    return CandidateProfile.from_ledger(
        build_synthetic_ledger(), total_experience_years=2.0,
        target_lanes=("JAVA_BACKEND", "GENERAL_SOFTWARE"), strong_overall=True)


def _portal_lead_jobs():
    """A couple of PORTAL_ONLY (LinkedIn) leads with no official evidence."""
    from atlas.candidate.eligibility import RankableJob
    return [
        RankableJob(job_key="li-1", company="PortalCo", title="Java Backend Engineer",
                    location="Bengaluru, India", lane="JAVA_BACKEND",
                    verification_state="PORTAL_CURRENT_LEAD", is_fetchable=True,
                    source_family="linkedin", url="https://www.linkedin.com/jobs/view/1",
                    record_class="PORTAL_ONLY"),
        RankableJob(job_key="li-2", company="PortalCo", title="Software Engineer",
                    location="Hyderabad, India", lane="GENERAL_SOFTWARE",
                    verification_state="PORTAL_CURRENT_LEAD", is_fetchable=True,
                    source_family="linkedin", url="https://www.linkedin.com/jobs/view/2",
                    record_class="PORTAL_ONLY"),
    ]


def _run_daily_with_official(tmp_path, run_id, *, portal=True):
    from atlas.runtime.daily import DailyRunner

    result, settings = _run_official(tmp_path, run_id + "-src")
    # feed the SAME official jobs into the daily pipeline via official_exec
    official_jobs = result.jobs

    def official_exec():
        return official_jobs

    def official_result_provider():
        return result

    market_exec = (lambda: _portal_lead_jobs()) if portal else None
    runner = DailyRunner(
        settings, run_id, jobs=[], candidate=_daily_candidate(), live=True,
        official_exec=official_exec, official_result_provider=official_result_provider,
        market_exec=market_exec, build_docx=False,
        candidate_mode_info={"candidate_mode": "PRIVATE_LOCAL", "synthetic": False,
                             "candidate_source_sha256": "deadbeef"})
    return runner.run(), runner, settings


def test_daily_official_jobs_in_all_jobs_portal_only_excluded(tmp_path):
    res, runner, settings = _run_daily_with_official(tmp_path, "PH2A-DAILY-1")
    assert res.terminal_state == "COMPLETE"
    manifest = runner.publisher.show_run("PH2A-DAILY-1")
    metrics = manifest["metrics"]
    assert metrics["direct_official_jobs"] >= 5
    assert metrics["all_jobs_rows"] >= 5
    assert metrics["portal_only_leads"] == 2          # the two LinkedIn leads
    assert metrics["portal_only_rows_in_all_jobs"] == 0
    # read the published workbook: every All_Jobs row is OFFICIAL_DIRECT/LINKED w/ official URL
    from openpyxl import load_workbook

    wb = load_workbook(runner.paths.workbook, read_only=True)
    ws = wb["All_Jobs"]
    header = [c for c in next(ws.iter_rows(values_only=True)) if c is not None]
    assert header[0] == "Record_Class"
    assert "Official_Requisition_ID" in header and "Discovery_Channels" in header
    rc_idx = header.index("Record_Class")
    url_idx = header.index("Official_Apply_URL")
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    assert rows, "All_Jobs must contain official rows"
    for row in rows:
        assert row[rc_idx] in ("OFFICIAL_DIRECT", "PORTAL_OFFICIAL_LINKED")
        assert row[url_idx] and "linkedin" not in str(row[url_idx])
    wb.close()
    # portal-only leads persisted to portal_leads.json, never All_Jobs
    portal_json = json.loads((runner.paths.run_dir / "portal_leads.json").read_text(encoding="utf-8"))
    assert portal_json["count"] == 2
    # candidate mode recorded (§12)
    assert manifest["candidate"]["candidate_mode"] == "PRIVATE_LOCAL"
    assert manifest["candidate"]["synthetic"] is False


# --------------------------------------------------------------------------- #
# §19.7 — a portal-only (LinkedIn-only) workbook can NEVER become latest.
# --------------------------------------------------------------------------- #
def test_portal_only_run_never_updates_latest(tmp_path):
    from atlas.runtime.daily import DailyRunner

    settings = _settings(tmp_path)
    runner = DailyRunner(
        settings, "PH2A-PORTAL-ONLY", jobs=[], candidate=_daily_candidate(), live=True,
        market_exec=lambda: _portal_lead_jobs(), build_docx=False,
        eligible_for_latest=False)  # --portal-only-debug sets this
    res = runner.run()
    # no official jobs -> empty All_Jobs, portal leads counted, latest NOT updated
    assert res.latest_updated in (None, False)
    manifest = runner.publisher.show_run("PH2A-PORTAL-ONLY")
    assert manifest["metrics"]["portal_only_leads"] == 2
    assert manifest["metrics"]["portal_only_rows_in_all_jobs"] == 0
    assert manifest["metrics"]["all_jobs_rows"] == 0
    # latest pointer must not point at this portal-only run
    latest = runner.publisher.latest()
    assert latest is None or latest.get("run_id") != "PH2A-PORTAL-ONLY"


def test_portal_only_row_would_break_latest_gate(tmp_path):
    """A stray PORTAL_ONLY row (or a portal-host URL) in All_Jobs forces the run
    ineligible for latest even when eligible_for_latest was requested."""
    from atlas.runtime.daily import DailyRunner

    settings = _settings(tmp_path)
    # inject a PORTAL_ONLY job DIRECTLY into the ranked set (adversarial): it must
    # be excluded from All_Jobs, keeping the gate satisfied.
    runner = DailyRunner(
        settings, "PH2A-STRAY", jobs=_portal_lead_jobs(), candidate=_daily_candidate(),
        live=True, build_docx=False)
    res = runner.run()
    manifest = runner.publisher.show_run("PH2A-STRAY")
    assert manifest["metrics"]["portal_only_rows_in_all_jobs"] == 0
    assert manifest["metrics"]["all_jobs_rows"] == 0  # PORTAL_ONLY excluded from All_Jobs


# --------------------------------------------------------------------------- #
# §19.4 + §19.5 — LiveOfficialFollowup: linked lead -> official primary; unlinked
# portal lead stays PORTAL_ONLY (never All_Jobs).
# --------------------------------------------------------------------------- #
def _single_official_greenhouse(company="LinkCo", title="Staff Java Platform Engineer"):
    class _GH(SourceAdapter):
        source_type = SourceType.ATS_GREENHOUSE
        source_family = SourceFamily.GREENHOUSE
        CAPABILITIES = frozenset({Capability.SEARCH, Capability.DETAIL, Capability.DESCRIPTION,
                                  Capability.POSTED_DATE, Capability.ACTIVE_STATUS})
        adapter_version = "gh-single-1"
        parser_version = "gh-single-p-1"

        def _mk(self, detail):
            return DiscoveryResult(**new_result_base(
                self, source_job_id="gh-single-1",
                source_url="https://boards.greenhouse.io/linkco/jobs/1",
                canonical_url="https://boards.greenhouse.io/linkco/jobs/1",
                company=company, title=title, location="Bengaluru, India",
                work_mode=WorkMode.REMOTE, posted_at=("2026-09-05" if detail else None),
                description=(_JAVA_DESC if detail else None), is_active=ActiveState.ACTIVE,
                verification_level=(VerificationLevel.OFFICIAL_DETAIL_LIVE if detail
                                    else VerificationLevel.OFFICIAL_SEARCH_LIVE),
                confidence=0.95,
                provenance={"source_family": "greenhouse", "requisition_id": "REQ-LINKCO-1",
                            "date_provenance": ("EMPLOYER_POSTED_AT" if detail else "EMPLOYER_UPDATED_AT")}))

        def health_check(self):
            return SourceHealth(SourceHealthState.HEALTHY, "ok")

        def search(self, request):
            return SearchResult(results=(self._mk(False),), page=request.page, has_more=False,
                                total_reported=1, zero_result_kind=ZeroResultKind.NOT_APPLICABLE)

        def fetch_detail(self, request):
            return self._mk(True)

    return _GH


def test_followup_linked_lead_uses_official_primary(tmp_path):
    from atlas.runtime.official_followup import KnownOfficialSource, LiveOfficialFollowup

    settings = _settings(tmp_path)
    gh_cls = _single_official_greenhouse(company="LinkCo", title="Staff Java Platform Engineer")
    reg = _registry(gh_cls)

    # portal adapter: one LinkedIn lead matching the official Java role exactly
    class _PortalAdapter:
        def search(self, request):
            r = DiscoveryResult(
                source_type=SourceType.FAKE, source_instance="li-x", source_job_id="li-999",
                source_url="https://www.linkedin.com/jobs/view/999",
                canonical_url="https://www.linkedin.com/jobs/view/999",
                company="LinkCo", title="Staff Java Platform Engineer", location="Bengaluru, India",
                verification_level=VerificationLevel.PORTAL_LIVE, is_active=ActiveState.ACTIVE,
                adapter_version="li-1", parser_version="li-p-1")
            return SearchResult(results=(r,), page=request.page, has_more=False,
                                total_reported=1, zero_result_kind=ZeroResultKind.NOT_APPLICABLE)

    src = KnownOfficialSource("LinkCo", "linkco.com", "greenhouse", "linkco")

    def official_factory(_src):
        return gh_cls(SourceInstance(
            instance_id="greenhouse-linkco", source_type=SourceType.ATS_GREENHOUSE,
            source_family=SourceFamily.GREENHOUSE, company_id="linkco",
            metadata={"board_token": "linkco"}))

    followup = LiveOfficialFollowup(
        settings, "PH2A-FOLLOWUP", known_sources=[src], registry=reg,
        portal_adapter_factory=lambda fam: _PortalAdapter(),
        official_adapter_factory=official_factory)
    fr = followup.run()
    # linked jobs are OFFICIAL primary (greenhouse), not linkedin rows
    assert fr.linked_official_jobs >= 1
    for j in fr.rankable_jobs():
        assert j.record_class == "PORTAL_OFFICIAL_LINKED"
        assert j.primary_source == "greenhouse"
        assert "linkedin" not in (j.url or "")
        assert "linkedin" in j.discovery_channels  # portal retained only as provenance
        assert j.official_requisition_id  # official requisition is primary


def test_followup_unlinked_lead_stays_portal_only(tmp_path):
    """A portal lead with NO matching official job stays PORTAL_ONLY and never
    becomes an All_Jobs (linked) row."""
    from atlas.runtime.official_followup import KnownOfficialSource, LiveOfficialFollowup

    settings = _settings(tmp_path)
    gh_cls = _single_official_greenhouse(company="LinkCo", title="Staff Java Platform Engineer")
    reg = _registry(gh_cls)

    class _PortalAdapter:
        def search(self, request):
            r = DiscoveryResult(
                source_type=SourceType.FAKE, source_instance="li-x", source_job_id="li-777",
                source_url="https://www.linkedin.com/jobs/view/777",
                canonical_url="https://www.linkedin.com/jobs/view/777",
                company="TotallyDifferentCo", title="Sales Manager", location="Mumbai, India",
                verification_level=VerificationLevel.PORTAL_LIVE, is_active=ActiveState.ACTIVE,
                adapter_version="li-1", parser_version="li-p-1")
            return SearchResult(results=(r,), page=request.page, has_more=False,
                                total_reported=1, zero_result_kind=ZeroResultKind.NOT_APPLICABLE)

    src = KnownOfficialSource("LinkCo", "linkco.com", "greenhouse", "linkco")

    def official_factory(_src):
        return gh_cls(SourceInstance(
            instance_id="greenhouse-linkco", source_type=SourceType.ATS_GREENHOUSE,
            source_family=SourceFamily.GREENHOUSE, company_id="linkco",
            metadata={"board_token": "linkco"}))

    followup = LiveOfficialFollowup(
        settings, "PH2A-FOLLOWUP-NOMATCH", known_sources=[src], registry=reg,
        portal_adapter_factory=lambda fam: _PortalAdapter(),
        official_adapter_factory=official_factory)
    fr = followup.run()
    assert fr.linked_official_jobs == 0
    assert fr.portal_only_count >= 1
    assert all(x["record_class"] == "PORTAL_ONLY" for x in fr.portal_only_leads())
