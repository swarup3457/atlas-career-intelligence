"""Agentic V4 — LangGraph runner + product validator, end-to-end offline (prompt s.13, s.15, s.16).

Drives the real runner (LangGraph governor, bounded concurrency, SQLite checkpointer, partial
resume, single reducer -> evaluate -> immutable report -> validate) with a CANNED worker (no
network, no LLM, no browser). The canned company results carry the exact four V3 Fiserv wrong
rows plus genuine India rows, so the real deterministic gates + product validator run for real.
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, field

import pytest

from atlas.candidate.search_profile import load_candidate_search_profile
from atlas.pilot.agentic_runner import AgenticRuntime, run_agentic_pilot
from atlas.pilot.config import load_pilot_config
from atlas.pilot.models import CompanySearchResult, JobDetailEvidence, LaneCoverage, PRIMARY_LANES
from atlas.pilot.status_v4 import CompanySearchStatus as S
from atlas.pilot.usage import UsageMeter

TODAY = datetime.date(2026, 9, 9)
RECENT = (TODAY - datetime.timedelta(days=3)).isoformat()
COMPANIES = ("Accenture", "Infosys", "TCS", "Cognizant", "IBM", "Oracle", "SAP",
             "JPMorgan Chase", "Fiserv", "ADP")


def _lanes():
    return {l: LaneCoverage(lane=l, attempted=True, board_snapshot_evaluated=True) for l in PRIMARY_LANES}


def _job(title, desc, exp, reqs, loc, url, i):
    return JobDetailEvidence(title=title, company="", location=loc, work_mode="ONSITE", description=desc,
                             mandatory_requirements=tuple(reqs), experience_text=exp, posted_date=RECENT,
                             updated_date=RECENT, requisition_id=f"R{i}", official_url=url,
                             eligibility_text=f"{loc}. {desc}", source_family="OFFICIAL_CAREERS",
                             evidence_snippets=(desc[:200],))


def _result(company, status, jobs, *, route="OFFICIAL_CAREERS", domain="x.com", entry="https://careers.x.com"):
    for j in jobs:
        j.company = company
    return CompanySearchResult(company=company, official_domain=domain, career_entry_url=entry, route=route,
                               source_family=route, status=status, lanes=_lanes(), jobs=list(jobs),
                               model="canned", task_id=f"T::{company}",
                               queries_attempted=["Java", "React", ".NET", "payroll"],
                               pages_or_interactions=len(jobs) + 4, tool_calls=len(jobs) + 5)


def _canned_results():
    fiserv_jobs = [
        _job("Tech Lead, Software Development Engineering",
             "Lead Java and React teams. 8&#43; years leading.",
             "8&#8211;12&#43; years of total hands-on software engineering experience", ("Java", "React"),
             "Pune, India", "https://fiserv.wd5.myworkdayjobs.com/EXT/job/tl", 1),
        _job(".NET Core Dev with SQL and Azure || Pune",
             "Enterprise applications in C#, .NET Core, ASP.NET. 4&#43; years Microsoft tech.",
             "4&#43; years of experience in software development using Microsoft technologies",
             (".NET", "C#", "ASP.NET"), "Pune, India", "https://fiserv.wd5.myworkdayjobs.com/EXT/job/net", 2),
        _job("Solutions Architecture - Advisor II", "Solution architecture advisory in Java. 6&#43; years.",
             "6&#43; years of architecture experience", ("Java", "TypeScript"), "Pune, India",
             "https://fiserv.wd5.myworkdayjobs.com/EXT/job/arch", 3),
    ]
    accenture_jobs = [
        _job("Java Backend Engineer", "Java Spring Boot microservices and REST APIs. 2 years experience.",
             "2 years experience", ("Java", "Spring Boot"), "Bengaluru, India",
             "https://www.accenture.com/in-en/careers/j1", 4),
    ]
    adp_jobs = [
        _job("Payroll Software Engineer", "Payroll and HCM integration in Java and Spring Boot. 2 years.",
             "2 years experience", ("Java", "Spring Boot", "payroll", "HCM"), "Hyderabad, India",
             "https://jobs.adp.com/j2", 5),
    ]
    jpmc_jobs = [
        _job("Java Developer", "Java backend on AWS. 3 years.", "3 years experience", ("Java", "Spring Boot"),
             "New York, United States", "https://careers.jpmorgan.com/j3", 6),
    ]
    return {
        "Accenture": _result("Accenture", S.SEARCHED_COMPLETE_WITH_MATCHES.value, accenture_jobs),
        "Infosys": _result("Infosys", S.ACCESS_LIMITED_EXTERNAL.value, []),
        "TCS": _result("TCS", S.SEARCHED_COMPLETE_NO_MATCHES.value, []),
        "Cognizant": _result("Cognizant", S.SEARCHED_COMPLETE_NO_MATCHES.value, []),
        "IBM": _result("IBM", S.SEARCHED_COMPLETE_NO_MATCHES.value, []),
        "Oracle": _result("Oracle", S.OFFICIAL_SOURCE_UNRESOLVED.value, []),
        "SAP": _result("SAP", S.SEARCHED_COMPLETE_NO_MATCHES.value, []),
        "JPMorgan Chase": _result("JPMorgan Chase", S.SEARCHED_COMPLETE_NO_MATCHES.value, jpmc_jobs),
        "Fiserv": _result("Fiserv", S.SEARCHED_COMPLETE_NO_MATCHES.value, fiserv_jobs, route="OFFICIAL_ATS_WORKDAY"),
        "ADP": _result("ADP", S.SEARCHED_COMPLETE_WITH_MATCHES.value, adp_jobs),
    }


@dataclass
class _CannedWorker:
    results: dict
    mode: str = "deterministic"
    profile_summary: dict = field(default_factory=dict)
    base_directory: object = None
    session_timeout_s: float = 1.0
    headless: bool = True
    browser_factory: object = None
    calls: dict = field(default_factory=dict)

    def search_company(self, task, usage):
        self.calls[task.company] = self.calls.get(task.company, 0) + 1
        import copy
        return copy.deepcopy(self.results[task.company])


@pytest.fixture(scope="module")
def config():
    cfg = load_pilot_config()
    from dataclasses import replace
    return replace(cfg, companies=COMPANIES)


@pytest.fixture(scope="module")
def profile():
    return load_candidate_search_profile(live=False, allow_synthetic=True)


def _runtime(tmp_path, config, profile, results, run_id="AGRUN"):
    worker = _CannedWorker(results=results)
    return AgenticRuntime(config=config, worker=worker, profile=profile,
                          output_root=tmp_path / "output" / "production", run_id=run_id, today=TODAY)


def test_ten_company_pass_and_validator_clean(tmp_path, config, profile):
    rt = _runtime(tmp_path, config, profile, _canned_results())
    out = run_agentic_pilot(rt)
    assert out.genuinely_searched == 8, out.genuinely_searched
    assert out.outcome == "PASS", out.outcome
    assert rt.validation["passed"] is True, rt.validation["failures"]
    # 0 wrong accepts: the four Fiserv rows never accepted; foreign JPMC row separated
    accepted_titles = {a["Role"] for a in json.loads((rt.report.run_dir / "jobs_accepted.json").read_text())}
    assert "Tech Lead, Software Development Engineering" not in accepted_titles
    assert "Solutions Architecture - Advisor II" not in accepted_titles
    assert ".NET Core Dev with SQL and Azure || Pune" not in accepted_titles
    foreign_titles = {f["Role"] for f in json.loads((rt.report.run_dir / "foreign_leads.json").read_text())}
    assert "Java Developer" in foreign_titles
    # immutable artifacts + never latest
    names = {p.name for p in rt.report.run_dir.iterdir()}
    assert {"jobs_accepted.json", "run_manifest.json", "company_results"} <= names
    assert not (tmp_path / "output" / "production" / "latest").exists()


def test_internal_error_forces_partial_not_pass(tmp_path, config, profile):
    results = _canned_results()
    # make one previously-searched company an internal retryable error (both attempts)
    results["TCS"] = _result("TCS", S.BROWSER_TOOL_ERROR.value, [])
    rt = _runtime(tmp_path, config, profile, results, run_id="AGRUN_INT")
    out = run_agentic_pilot(rt)
    assert out.outcome == "PARTIAL", out.outcome  # internal error may never be terminal under PASS
    assert out.genuinely_searched == 7


def test_resume_skips_completed_terminal(tmp_path, config, profile):
    results = _canned_results()
    rt1 = _runtime(tmp_path, config, profile, results, run_id="AGRUN_RES")
    run_agentic_pilot(rt1)
    first_calls = dict(rt1.worker.calls)
    assert first_calls  # ran companies

    # a second runtime over the SAME run dir: a boom worker must NOT be called for
    # already-terminal companies (they resume from the partial store).
    @dataclass
    class _Boom:
        mode: str = "deterministic"
        profile_summary: dict = field(default_factory=dict)
        base_directory: object = None
        session_timeout_s: float = 1.0
        headless: bool = True
        browser_factory: object = None

        def search_company(self, task, usage):
            raise AssertionError(f"should not re-run terminal company {task.company}")

    rt2 = AgenticRuntime(config=config, worker=_Boom(), profile=profile,
                         output_root=tmp_path / "output" / "production", run_id="AGRUN_RES", today=TODAY)
    out2 = run_agentic_pilot(rt2)
    assert out2.genuinely_searched == 8


def test_empty_or_nonjob_title_never_accepted(config, profile):
    """A captured record with no usable job title (empty, or a banner) must be
    dropped deterministically before adjudication, on ANY route incl. the ATS
    fast path — the source-side backstop to the product validator (pass-4)."""
    from atlas.hunt.role_intent import load_role_intent_policy
    from atlas.policy.loader import load_policy
    from atlas.pilot.evaluate import evaluate_pilot
    lanes = _lanes()
    jobs = [
        _job("", "Build Java Spring Boot microservices. 2 years experience.", "2 years",
             ("Java", "Spring Boot"), "Bengaluru, India", "https://x/1", 1),
        _job("YOU ARE ONE STEP CLOSER", "Java role. 2 years.", "2 years", ("Java",),
             "Hyderabad, India", "https://x/2", 2),
        _job("Java Backend Engineer", "Build Java Spring Boot microservices and REST APIs. 2 years.",
             "2 years", ("Java", "Spring Boot"), "Bengaluru, India", "https://x/3", 3),
    ]
    res = _result("Accenture", S.SEARCHED_COMPLETE_WITH_MATCHES.value, jobs)
    ev = evaluate_pilot([res], load_role_intent_policy(), load_policy(), profile, today=TODAY)
    accepted_titles = [a.title for a in ev.accepted]
    assert accepted_titles == ["Java Backend Engineer"]
    assert any(r.reason_code == "REJECT_NON_JOB_CAPTURE" for r in ev.rejected)

    from atlas.hunt.role_intent import load_role_intent_policy
    from atlas.policy.loader import load_policy
    from atlas.pilot.evaluate import evaluate_pilot
    from atlas.pilot.agentic_validator import validate_agentic_run

    # a hard-8+ Java role wrongly marked as a clean India row cannot pass the gates,
    # so the validator must not see it accepted; conversely a clean run passes.
    results = list(_canned_results().values())
    intent = load_role_intent_policy()
    policy = load_policy()
    ev = evaluate_pilot(results, intent, policy, profile, today=TODAY)
    v = validate_agentic_run(ev, results, config, outcome="PASS", candidate_synthetic=True)
    assert v["passed"] is True, v["failures"]
    # forcing PASS with only 5 searched must fail the >=8 rule
    few = [r for r in results][:5]
    v2 = validate_agentic_run(ev, few, config, outcome="PASS", candidate_synthetic=True)
    assert v2["passed"] is False
