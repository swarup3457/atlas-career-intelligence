"""Deterministic company-discovery fixtures (Phase 1A.5).

Network-free fixture observations covering the required discovery cases
(Greenhouse / Workday / Lever / custom / multiple ATS / migration /
lookalike / untrusted / malformed) plus a mixed 20-company workload for the
LangGraph integration test. These are test data, NOT real companies.
"""

from __future__ import annotations

from atlas.company.models import CompanyObservation, DiscoveryMethod

_C = DiscoveryMethod.CONFIRMED_IDENTITY


def fixture_company_cases() -> dict[str, CompanyObservation]:
    """Named fixture cases A–G used by the discovery tests."""
    return {
        # A: known domain → Greenhouse
        "A_greenhouse": CompanyObservation(
            name="Acme", official_domain="acme.com",
            careers_url="https://boards.greenhouse.io/acme", method=_C,
        ),
        # B: career URL → Workday (via redirect)
        "B_workday": CompanyObservation(
            name="Globex", official_domain="globex.com",
            careers_url="https://globex.com/careers",
            redirect_url="https://globex.wd1.myworkdayjobs.com/External",
            method=DiscoveryMethod.VALIDATED_REDIRECT,
        ),
        # C: redirect → Lever
        "C_lever": CompanyObservation(
            name="Initech", careers_url="https://jobs.lever.co/initech",
            method=DiscoveryMethod.CANDIDATE_URL,
        ),
        # D: unknown custom careers page
        "D_custom": CompanyObservation(
            name="Umbrella", official_domain="umbrella.co",
            careers_url="https://careers.umbrella.co/openings", method=_C,
        ),
        # E: one company, multiple ATS
        "E_multi_1": CompanyObservation(
            name="MultiCo", official_domain="multico.com",
            careers_url="https://multico.wd1.myworkdayjobs.com/Global", method=_C,
        ),
        "E_multi_2": CompanyObservation(
            name="MultiCo", official_domain="multico.com",
            careers_url="https://boards.greenhouse.io/multicoindia", method=_C,
        ),
        # F: ATS migration old→new
        "F_migration_old": CompanyObservation(
            name="OldCo", official_domain="oldco.com",
            careers_url="https://oldco.taleo.net/careersection/x", method=_C,
        ),
        "F_migration_new": CompanyObservation(
            name="OldCo", official_domain="oldco.com",
            careers_url="https://oldco.wd1.myworkdayjobs.com/New", method=_C,
        ),
        # G: similar name, different domain (must NOT merge)
        "G_lookalike_1": CompanyObservation(name="Delta", official_domain="delta-air.com", method=_C),
        "G_lookalike_2": CompanyObservation(name="Delta", official_domain="delta-faucet.com", method=_C),
        # untrusted posting URL (domain safety)
        "untrusted": CompanyObservation(
            name="Phish Inc", careers_url="https://phish.example/register",
            method=DiscoveryMethod.UNTRUSTED_POSTING,
        ),
    }


def build_discovery_workload() -> tuple[dict[str, CompanyObservation], set[str]]:
    """A deterministic mixed 20-item company-discovery workload for the
    LangGraph integration test. Returns (plan, transient_failure_items).

    Includes: new, existing, duplicate, alias, multiple-source, unknown-ATS,
    migration, malformed observation, and one retryable transient failure.
    """
    plan: dict[str, CompanyObservation] = {}

    plan["new-greenhouse"] = CompanyObservation(
        name="Acme", official_domain="acme.com",
        careers_url="https://boards.greenhouse.io/acme", method=_C,
    )
    # duplicate of the same company (same domain) — must not create a 2nd company
    plan["dup-acme"] = CompanyObservation(
        name="Acme Inc.", official_domain="acme.com",
        careers_url="https://boards.greenhouse.io/acme", method=_C,
    )
    # alias variant of the same company (different display name, same domain)
    plan["alias-acme"] = CompanyObservation(
        name="Acme Corporation Global", official_domain="acme.com", method=_C,
    )
    plan["new-workday"] = CompanyObservation(
        name="Globex", official_domain="globex.com",
        careers_url="https://globex.wd1.myworkdayjobs.com/External", method=_C,
    )
    plan["new-lever"] = CompanyObservation(
        name="Initech", careers_url="https://jobs.lever.co/initech",
        method=DiscoveryMethod.CANDIDATE_URL,
    )
    plan["new-smartrecruiters"] = CompanyObservation(
        name="Hooli", official_domain="hooli.com",
        careers_url="https://careers.smartrecruiters.com/Hooli", method=_C,
    )
    plan["unknown-ats"] = CompanyObservation(
        name="Umbrella", official_domain="umbrella.co",
        careers_url="https://careers.umbrella.co/openings", method=_C,
    )
    # multiple ATS for one company
    plan["multi-1"] = CompanyObservation(
        name="MultiCo", official_domain="multico.com",
        careers_url="https://multico.wd1.myworkdayjobs.com/Global", method=_C,
    )
    plan["multi-2"] = CompanyObservation(
        name="MultiCo", official_domain="multico.com",
        careers_url="https://boards.greenhouse.io/multicoindia", method=_C,
    )
    # migration pair
    plan["mig-old"] = CompanyObservation(
        name="OldCo", official_domain="oldco.com",
        careers_url="https://oldco.taleo.net/careersection/x", method=_C,
    )
    plan["mig-new"] = CompanyObservation(
        name="OldCo", official_domain="oldco.com",
        careers_url="https://oldco.wd1.myworkdayjobs.com/New", method=_C,
    )
    # lookalikes (different domains, must not merge)
    plan["look-1"] = CompanyObservation(name="Delta", official_domain="delta-air.com", method=_C)
    plan["look-2"] = CompanyObservation(name="Delta", official_domain="delta-faucet.com", method=_C)
    # untrusted posting URL
    plan["untrusted"] = CompanyObservation(
        name="Phish Inc", careers_url="https://phish.example/register",
        method=DiscoveryMethod.UNTRUSTED_POSTING,
    )
    # malformed observation (empty name)
    plan["malformed"] = CompanyObservation(name="", method=_C)
    # unicode company name
    plan["unicode"] = CompanyObservation(
        name="Café Solutions Private Limited", official_domain="cafesolutions.in",
        careers_url="https://boards.greenhouse.io/cafesolutions", method=_C,
    )
    # a few plain new companies to reach 20
    plan["plain-1"] = CompanyObservation(name="Stark Industries", official_domain="stark.example",
                                          careers_url="https://jobs.lever.co/stark", method=_C)
    plan["plain-2"] = CompanyObservation(name="Wayne Enterprises", official_domain="wayne.example",
                                          careers_url="https://boards.greenhouse.io/wayne", method=_C)
    plan["plain-3"] = CompanyObservation(name="Wonka", official_domain="wonka.example", method=_C)
    plan["transient"] = CompanyObservation(name="Cyberdyne", official_domain="cyberdyne.example",
                                            careers_url="https://cyberdyne.wd1.myworkdayjobs.com/Jobs", method=_C)

    transient_failures = {"transient"}
    return plan, transient_failures


__all__ = ["fixture_company_cases", "build_discovery_workload"]
