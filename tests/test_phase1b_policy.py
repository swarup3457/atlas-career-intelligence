"""Phase 1B — policy import tests (build spec sections 9-14, 17, 18, 24)."""

from __future__ import annotations

import datetime

import pytest

from atlas.policy import PolicyBundle, PolicyValidationError, load_policy
from atlas.policy.rules import (
    ELIGIBLE_FROM_INDIA,
    ELIGIBLE_WITH_EVIDENCE,
    NOT_ELIGIBLE,
    UNCLEAR,
    VerificationInput,
    classify_verification,
    evaluate_exclusions,
    experience_eligible,
    extract_experience,
    freshness_band,
    international_eligibility,
    is_closed,
)
from atlas.policy.status import (
    JobLifecycleStatus,
    RecommendationStatus,
    VerificationLevel,
    combined_verification_display,
    normalize_lifecycle,
    normalize_recommendation,
    normalize_verification,
)

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def bundle() -> PolicyBundle:
    return load_policy()


# --- loading + fingerprint -------------------------------------------------
def test_policy_loads_and_fingerprints(bundle):
    assert bundle.fingerprint
    assert bundle.short_fingerprint
    for name in ("lanes", "geography", "experience", "exclusions", "cadence",
                 "source_policy", "company_seed", "verification"):
        assert name in bundle.loaded
        assert bundle.loaded[name].policy_version


# --- six independent lanes -------------------------------------------------
def test_all_six_lanes_present_and_distinct(bundle):
    keys = set(bundle.lanes)
    assert keys == {
        "GENERAL_SOFTWARE", "JAVA_BACKEND", "JAVA_FULLSTACK",
        "REACT_FRONTEND", "DOTNET", "ENTERPRISE_HR_PAYROLL_INTEGRATION",
    }


def test_react_lane_has_no_java_terms(bundle):
    react = bundle.lane("REACT_FRONTEND")
    tech = " ".join(react.technology_terms).lower()
    assert "java" not in tech.replace("javascript", "")  # java (not javascript) absent
    assert any("react" in t.lower() for t in react.technology_terms)


def test_dotnet_lane_discoverable(bundle):
    dotnet = bundle.lane("DOTNET")
    tech = " ".join(dotnet.technology_terms).lower()
    assert ".net" in tech and "c#" in tech


def test_enterprise_lane_independent(bundle):
    ent = bundle.lane("ENTERPRISE_HR_PAYROLL_INTEGRATION")
    tech = " ".join(ent.technology_terms).lower()
    assert "payroll" in tech and "integration" in tech


def test_java_backend_and_fullstack_not_collapsed(bundle):
    be = bundle.lane("JAVA_BACKEND")
    fs = bundle.lane("JAVA_FULLSTACK")
    assert be.key != fs.key
    assert "react" not in " ".join(be.technology_terms).lower()
    assert "react" in " ".join(fs.technology_terms).lower()


# --- geography -------------------------------------------------------------
def test_geo_aliases_normalize(bundle):
    g = bundle.geography
    assert g.normalize("Bangalore").canonical == "Bengaluru"
    assert g.normalize("Bengaluru").canonical == "Bengaluru"
    assert g.normalize("Gurgaon").canonical == "Gurugram"
    assert g.normalize("Mysore").canonical == "Mysuru"
    assert g.group_for("Bengaluru") == "PRIMARY"
    assert g.group_for("Pune") == "SECONDARY"
    assert g.group_for("Kochi") == "EXPANSION"


def test_remote_alone_is_not_worldwide(bundle):
    g = bundle.geography
    assert international_eligibility("Remote", g) == UNCLEAR
    assert international_eligibility("US Remote only", g) == NOT_ELIGIBLE
    assert international_eligibility("Remote, worldwide including India", g) == ELIGIBLE_WITH_EVIDENCE
    assert international_eligibility("visa sponsorship available", g) == ELIGIBLE_WITH_EVIDENCE
    assert international_eligibility("Bengaluru, India", g) == ELIGIBLE_FROM_INDIA


# --- experience ------------------------------------------------------------
def test_hard_four_plus_minimum_rejects(bundle):
    ex = extract_experience("Minimum 4 years of experience required")
    assert ex.min_years == 4
    assert experience_eligible(ex, bundle.experience).eligible is False

    ex2 = extract_experience("4+ years")
    assert experience_eligible(ex2, bundle.experience).eligible is False


def test_senior_title_survives_when_requirements_fit(bundle):
    # Title says Senior but the mandatory experience is 2-3 years -> eligible.
    ex = extract_experience("Senior Software Engineer, 2-3 years experience")
    assert ex.min_years == 2 and ex.max_years == 3
    assert experience_eligible(ex, bundle.experience).eligible is True


def test_junior_title_fails_when_mandatory_experience_excessive(bundle):
    ex = extract_experience("Junior Developer but requires 8+ years")
    assert ex.min_years == 8
    assert experience_eligible(ex, bundle.experience).eligible is False


def test_title_alone_is_ambiguous_not_a_range(bundle):
    ex = extract_experience("Software Engineer")
    assert ex.ambiguous is True
    assert ex.min_years is None  # never invent a range from a title


# --- exclusions ------------------------------------------------------------
def test_exclusions_reject_negative_families(bundle):
    assert evaluate_exclusions("Manual testing role, test case execution only", bundle.exclusions).excluded
    assert evaluate_exclusions("BPO voice process", bundle.exclusions).excluded


def test_exclusions_do_not_overfilter_dev_role(bundle):
    text = "Backend Java engineer; participates in production support and on-call rotation"
    assert evaluate_exclusions(text, bundle.exclusions).excluded is False


# --- company seed ----------------------------------------------------------
def test_exactly_108_seed_companies(bundle):
    assert len(bundle.company_seed.companies) == 108
    names = bundle.company_seed.names()
    assert len(set(names)) == 108  # no duplicates
    assert "Microsoft" in names and "Infosys" in names


def test_seed_is_not_a_whitelist(bundle):
    assert bundle.company_seed.is_whitelist is False


# --- cadence ---------------------------------------------------------------
def test_cadence_tier_values(bundle):
    assert (bundle.cadence.tiers["A"].delta_days, bundle.cadence.tiers["A"].deep_days) == (1, 2)
    assert (bundle.cadence.tiers["B"].delta_days, bundle.cadence.tiers["B"].deep_days) == (2, 3)
    assert (bundle.cadence.tiers["C"].delta_days, bundle.cadence.tiers["C"].deep_days) == (2, 5)
    assert bundle.cadence.batch_size_hint == 40  # a batch hint, not a completion cap


# --- source policy ---------------------------------------------------------
def test_source_policy_has_no_live_adapters(bundle):
    assert bundle.source_policy.entries
    assert all(not e.live_adapter for e in bundle.source_policy.entries)
    families = {e.family for e in bundle.source_policy.entries}
    assert {"linkedin", "naukri", "ashby", "workday", "wellfound"} <= families


# --- verification / freshness / closure ------------------------------------
def test_verified_official_without_final_apply(bundle):
    assert bundle.verification.require_final_apply_submission is False
    vi = VerificationInput(page_kind="specific_role_page", identity_aligned=True, current_content=True)
    assert classify_verification(vi) == VerificationLevel.VERIFIED_OFFICIAL


def test_generic_landing_page_cannot_verify_specific_role():
    vi = VerificationInput(page_kind="generic_landing", identity_aligned=True, current_content=True)
    assert classify_verification(vi) != VerificationLevel.VERIFIED_OFFICIAL


def test_portal_lead_is_not_official_verification():
    vi = VerificationInput(page_kind="portal", identity_aligned=True, current_content=True)
    assert classify_verification(vi) == VerificationLevel.PORTAL_CURRENT_LEAD


def test_apply_path_unconfirmed_when_not_confirmable():
    vi = VerificationInput(page_kind="specific_role_page", identity_aligned=True, current_content=True, apply_path_confirmable=False)
    assert classify_verification(vi) == VerificationLevel.OFFICIAL_PAGE_FOUND_APPLY_PATH_UNCONFIRMED


def test_blocked_login_captcha_is_not_closed(bundle):
    assert is_closed("Login required, CAPTCHA challenge, JavaScript failure", bundle.verification) is False
    assert is_closed("access limited, missing date", bundle.verification) is False


def test_positive_closure_evidence_closes(bundle):
    assert is_closed("This position has been filled and applications closed", bundle.verification) is True
    assert is_closed("No longer accepting applications", bundle.verification) is True


def test_missing_date_is_not_stale():
    assert freshness_band(None, has_live_official_page=True) == "LIVE_DATE_UNKNOWN"
    assert freshness_band(None, has_live_official_page=False) == "LIVE_DATE_UNKNOWN"


def test_freshness_bands():
    today = datetime.date(2026, 1, 31)
    assert freshness_band(datetime.date(2026, 1, 28), today=today) == "0-7 days"
    assert freshness_band(datetime.date(2026, 1, 20), today=today) == "8-14 days"
    assert freshness_band(datetime.date(2026, 1, 5), today=today) == "15-30 days"
    assert freshness_band(datetime.date(2025, 12, 1), today=today) == "STALE"


# --- status axes -----------------------------------------------------------
def test_status_axes_separated_and_aliased():
    assert normalize_verification("Verified Official") == VerificationLevel.VERIFIED_OFFICIAL
    assert normalize_verification("PORTAL_ONLY_UNVERIFIED") == VerificationLevel.PORTAL_CURRENT_LEAD
    assert normalize_lifecycle("Expired") == JobLifecycleStatus.CLOSED
    assert normalize_recommendation("Apply Now") == RecommendationStatus.PRIORITY_APPLY
    # CLOSED is lifecycle, not verification: no CLOSED member on VerificationLevel
    assert not hasattr(VerificationLevel, "CLOSED")


def test_combined_verification_display_derives_closed():
    disp = combined_verification_display(VerificationLevel.VERIFIED_OFFICIAL, JobLifecycleStatus.CLOSED)
    assert disp == "CLOSED"
    disp2 = combined_verification_display(VerificationLevel.VERIFIED_OFFICIAL, JobLifecycleStatus.ACTIVE)
    assert disp2 == "VERIFIED_OFFICIAL"


# --- validation guards -----------------------------------------------------
def test_missing_lane_fails_validation(tmp_path):
    import shutil
    from atlas.policy.loader import DEFAULT_POLICY_DIR

    dst = tmp_path / "policy"
    shutil.copytree(DEFAULT_POLICY_DIR, dst)
    # Corrupt lanes: drop a required lane
    lanes_file = dst / "search_lanes.yaml"
    text = lanes_file.read_text(encoding="utf-8").replace("  DOTNET:", "  DOTNET_DISABLED:")
    lanes_file.write_text(text, encoding="utf-8")
    with pytest.raises(PolicyValidationError):
        load_policy(dst)
