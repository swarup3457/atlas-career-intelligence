"""Agentic V4 recovery — failing-first semantic regressions (prompt s.3.2, s.3.3, s.7).

These lock in the EXACT V3 Fiserv failures the deep audit identified:

* numeric HTML entities (``4&#43; years``) survived stripping, so a hard 4+/6+/
  8-12+ mandatory minimum fell through as *ambiguous* -> *eligible*;
* a preferred ``5+`` was not distinguished from a mandatory minimum;
* ``Solutions Architecture - Advisor II`` was accepted as Java full stack;
* ``Tech Lead, ...`` (8-12+) was not rejected on role family;
* a ``.NET`` role was mislabeled ``ENTERPRISE_HR_PAYROLL_INTEGRATION`` on the
  generic phrase "enterprise applications" with no payroll/HCM/HRIS evidence;
* an alternative-language enumeration ("Python, TypeScript, Java, or Go") was
  treated as a mandatory Java backend.

Written before the fix; they must fail on the V3 code and pass after V4.
"""

from __future__ import annotations

import datetime

import pytest

from atlas.hunt.pipeline import evaluate_detail
from atlas.hunt.models import JobDetailRevision
from atlas.hunt.qualification import QualificationStatus
from atlas.hunt.role_family import RoleFamily, classify_role_family
from atlas.hunt.role_intent import load_role_intent_policy
from atlas.policy.loader import load_policy
from atlas.policy.rules import extract_experience
from atlas.pilot.normalize import normalize_source_text, split_sections

TODAY = datetime.date(2026, 9, 9)
CANDIDATE_YEARS = 2.0  # the configured early-career candidate does NOT satisfy 4+


@pytest.fixture(scope="module")
def policy():
    return load_policy()


@pytest.fixture(scope="module")
def intent():
    return load_role_intent_policy()


def _detail(title, *, desc, exp, reqs=(), pref=(), location="Pune - Trion Business Park, India"):
    return JobDetailRevision(
        revision_id="R", snapshot_id="S", source_job_id="J", company="Fiserv",
        title=title, description=desc, mandatory_requirements=tuple(reqs),
        preferred_requirements=tuple(pref), experience_text=exp, location=location,
        posted_date=TODAY, official_url="https://fiserv.wd5.myworkdayjobs.com/EXT/job/x",
        requisition_id="R-1", verification_state="VERIFIED_OFFICIAL", has_live_official_page=True,
        eligibility_text=f"{location}. {desc}", source_family="OFFICIAL_ATS_WORKDAY",
    )


# ---------------------------------------------------------------------------
# s.3.2 — HTML-entity + mandatory/preferred experience extraction
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected_min",
    [
        ("8&#8211;12&#43; years of total hands-on software engineering experience", 8.0),
        ("4&#43; years of experience in software development using Microsoft technologies", 4.0),
        ("6&#43; years of architecture experience", 6.0),
        ("2&#43; years required, 5&#43; preferred", 2.0),
    ],
)
def test_numeric_entity_experience_minimum(raw, expected_min):
    ex = extract_experience(raw)
    assert ex.min_years == expected_min, f"{raw!r} -> {ex}"


@pytest.mark.parametrize(
    "raw,expected_min,expected_max",
    [
        # The exact live IBM shapes that pass-4 JD capture surfaced (label-prefixed).
        ("...scalability, and automation. Years of Experience:6 - 7 ABOUT BUSINESS", 6.0, 7.0),
        ("...automation. Years of Experience:5 - 10 ABOUT BUSINESS UNIT", 5.0, 10.0),
        ("Years of Experience: 8+", 8.0, None),
        ("Experience: 5-10 years", 5.0, 10.0),
        ("Experience Required: 6 years", 6.0, None),
        ("Minimum Years of Experience: 4", 4.0, None),
    ],
)
def test_label_prefixed_experience_minimum(raw, expected_min, expected_max):
    ex = extract_experience(raw)
    assert ex.min_years == expected_min, f"{raw!r} -> {ex}"
    if expected_max is not None:
        assert ex.max_years == expected_max, f"{raw!r} -> {ex}"


def test_label_prefixed_senior_role_rejected_via_gate():
    """A live-IBM-style '6 - 7 years' full-stack role must REJECT on experience for
    the early-career candidate (0 false positives, prompt s.15)."""
    import datetime
    from atlas.hunt.models import JobDetailRevision
    from atlas.hunt.pipeline import evaluate_detail
    from atlas.hunt.qualification import QualificationStatus
    from atlas.hunt.role_intent import load_role_intent_policy
    from atlas.policy.loader import load_policy

    today = datetime.date(2026, 9, 9)
    d = JobDetailRevision(
        revision_id="R", snapshot_id="S", source_job_id="J", company="IBM",
        title="Software Engineering Application Developer-Cloud FullStack Professional",
        description=("Work with multiple technologies, including Angular, React, CSS3, HTML5, Java, "
                     "JEE, Spring, Hibernate, REST services for scalability and automation. "
                     "Years of Experience:6 - 7 ABOUT BUSINESS UNIT."),
        mandatory_requirements=("Java", "Spring", "React"),
        experience_text="Years of Experience:6 - 7",
        location="Bangalore, India", posted_date=today,
        official_url="https://careers.ibm.com/job/1", requisition_id="R1",
        verification_state="VERIFIED_OFFICIAL", has_live_official_page=True,
        eligibility_text="Bangalore, India.", source_family="OFFICIAL_CAREERS_BROWSER",
    )
    ev = evaluate_detail(d, load_role_intent_policy(), load_policy(), candidate_years=2.0, today=today)
    assert ev.final_status == QualificationStatus.REJECT_EXPERIENCE.value, ev.final_status


def test_preferred_plus_not_read_as_mandatory():
    ex = extract_experience("2&#43; years required, 5&#43; preferred")
    assert ex.min_years == 2.0
    assert ex.preferred_years == 5.0


def test_normalizer_decodes_entities_and_dashes():
    assert normalize_source_text("8&#8211;12&#43; years") == "8-12+ years"
    assert normalize_source_text("4&#43; years") == "4+ years"


def test_section_split_separates_preferred_from_mandatory():
    st = split_sections(
        "Minimum Qualifications\n4&#43; years Java\n"
        "Preferred Qualifications\n8&#43; years leadership preferred"
    )
    assert extract_experience(st.mandatory_text).min_years == 4.0
    # the preferred 8+ must not leak in as a mandatory minimum
    assert extract_experience(st.mandatory_text).min_years != 8.0


# ---------------------------------------------------------------------------
# s.3.2 — the exact four V3 Fiserv rows resolve correctly through the gate
# ---------------------------------------------------------------------------
def test_fiserv_tech_lead_8_12_rejected(policy, intent):
    d = _detail(
        "Tech Lead, Software Development Engineering",
        desc="Lead a team building Java and React services. 8&#43; years leading engineering teams.",
        exp="8&#8211;12&#43; years of total hands-on software engineering experience",
        reqs=("Java", "React"),
    )
    ev = evaluate_detail(d, intent, policy, candidate_years=CANDIDATE_YEARS, today=TODAY)
    assert ev.final_status in (
        QualificationStatus.REJECT_EXPERIENCE.value,
        QualificationStatus.REJECT_ROLE_FAMILY.value,
    ), ev.final_status


def test_fiserv_dotnet_is_dotnet_lane_and_experience_rejected(policy, intent):
    d = _detail(
        ".NET Core Dev with SQL and Azure || Pune",
        desc=("Develop enterprise applications using C#, .NET Core, ASP.NET, SQL Server and Azure. "
              "4&#43; years of experience in software development using Microsoft technologies."),
        exp="4&#43; years of experience in software development using Microsoft technologies",
        reqs=(".NET", "C#", "ASP.NET"),
    )
    ev = evaluate_detail(d, intent, policy, candidate_years=CANDIDATE_YEARS, today=TODAY)
    # must NOT be accepted, and the lane must be DOTNET, never HR/payroll
    assert ev.final_status == QualificationStatus.REJECT_EXPERIENCE.value, ev.final_status
    lanes = ev.qualification.by_lane
    assert "ENTERPRISE_HR_PAYROLL_INTEGRATION" in lanes
    assert not lanes["ENTERPRISE_HR_PAYROLL_INTEGRATION"].qualified
    # DOTNET is the relevant lane whose experience gate fired
    assert lanes["DOTNET"].status == QualificationStatus.REJECT_EXPERIENCE.value


def test_fiserv_solutions_architect_advisor_rejected_role_family(policy, intent):
    d = _detail(
        "Solutions Architecture - Advisor II",
        desc="Provide solution architecture advisory across Java and TypeScript platforms. 6&#43; years.",
        exp="6&#43; years of architecture experience",
        reqs=("Java", "TypeScript"),
    )
    ev = evaluate_detail(d, intent, policy, candidate_years=CANDIDATE_YEARS, today=TODAY)
    assert ev.final_status in (
        QualificationStatus.REJECT_ROLE_FAMILY.value,
        QualificationStatus.REJECT_EXPERIENCE.value,
    ), ev.final_status
    assert classify_role_family("Solutions Architecture - Advisor II").family == \
        RoleFamily.ARCHITECTURE_ADVISORY.value


def test_fiserv_sr_professional_unknown_years_is_manual_not_strong(policy, intent):
    d = _detail(
        "Software Development Engineering - Sr Professional I",
        desc="Design and build Java, Spring Boot, React and Angular systems.",
        exp="Experience across Java and modern frontend frameworks.",
        reqs=("Java", "Spring Boot", "React"),
    )
    ev = evaluate_detail(d, intent, policy, candidate_years=CANDIDATE_YEARS, today=TODAY)
    if ev.final_status == QualificationStatus.QUALIFIED.value and ev.final_lane:
        fit = ev.qualification.by_lane[ev.final_lane].experience_fit
        assert fit in ("MANUAL_VERIFICATION", None), fit


# ---------------------------------------------------------------------------
# s.3.3 — role family + lane gates
# ---------------------------------------------------------------------------
def test_tech_lead_role_family():
    assert classify_role_family("Tech Lead, Software Development Engineering").family == \
        RoleFamily.TECH_LEAD.value


def test_plain_software_engineer_not_over_excluded():
    assert classify_role_family("Software Engineer").family in (
        RoleFamily.SOFTWARE_DEVELOPMENT.value,
    )
    # a senior title is NOT a role-family exclusion; the experience gate handles years
    assert classify_role_family("Senior Software Engineer").family in (
        RoleFamily.SOFTWARE_DEVELOPMENT.value,
    )


def test_dotnet_role_not_hr_payroll_without_domain(policy, intent):
    d = _detail(
        "Software Engineer",
        desc="Build enterprise applications in C#, .NET and ASP.NET. 2 years experience.",
        exp="2 years experience", reqs=(".NET", "C#"),
        location="Bengaluru, India",
    )
    ev = evaluate_detail(d, intent, policy, candidate_years=CANDIDATE_YEARS, today=TODAY)
    lanes = ev.qualification.by_lane
    assert not lanes["ENTERPRISE_HR_PAYROLL_INTEGRATION"].qualified
    assert lanes["DOTNET"].qualified


def test_hr_payroll_positive_needs_domain_evidence(policy, intent):
    d = _detail(
        "Payroll Software Engineer",
        desc="Build payroll and HCM integration services in Java and Spring Boot. 2 years experience.",
        exp="2 years experience", reqs=("Java", "Spring Boot", "payroll", "HCM"),
        location="Hyderabad, India",
    )
    ev = evaluate_detail(d, intent, policy, candidate_years=CANDIDATE_YEARS, today=TODAY)
    assert ev.final_status == QualificationStatus.QUALIFIED.value
    assert ev.final_lane == "ENTERPRISE_HR_PAYROLL_INTEGRATION"


def test_alternative_language_group_not_mandatory_java(policy, intent):
    # Java appears ONLY as one of several alternative prototype languages.
    d = _detail(
        "Software Engineer, Prototyping",
        desc=("Prototype ideas quickly. You may use Python, TypeScript, Java, or Go. "
              "Strong CS fundamentals. 2 years experience."),
        exp="2 years experience", reqs=(),
        location="Bengaluru, India",
    )
    ev = evaluate_detail(d, intent, policy, candidate_years=CANDIDATE_YEARS, today=TODAY)
    # It must not qualify as a Java backend/full-stack role on an alt-language mention.
    if ev.final_status == QualificationStatus.QUALIFIED.value:
        assert ev.final_lane not in ("JAVA_BACKEND", "JAVA_FULLSTACK")
