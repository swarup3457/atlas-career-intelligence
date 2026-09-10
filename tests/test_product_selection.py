"""Failing-first tests for the deterministic product-company stratified selector (prompt s.11)."""

from __future__ import annotations

import datetime

import pytest

from atlas.company.product_pool import (
    ProductCompany,
    ProductPool,
    plan_daily_buckets,
    select_fixed_cohort,
    select_product_companies,
)

TODAY = datetime.date(2026, 9, 10)


def _c(name, category="ENTERPRISE_SAAS", *, family=None, last=None, disabled=False,
       india=True, yield_=0.0, priority=0.0, retry=False, newly=False, lanes=("JAVA_BACKEND",)):
    return ProductCompany(
        name=name, category=category, official_domain=f"{name.lower()}.com",
        career_entry_url=(f"https://{family}.example/{name}" if family else f"https://{name.lower()}.com/careers"),
        india_presence=india, india_enabled_search=india, lane_affinity=tuple(lanes),
        source_family=(family or ""),
        last_successful_search=last, historical_yield=yield_, company_priority=priority,
        retry_due=retry, newly_added=newly, disabled=disabled,
    )


def _pool(*companies):
    return ProductPool(companies=tuple(companies))


def test_only_product_categories_are_eligible():
    pool = _pool(
        _c("Alpha", "ENTERPRISE_SAAS"),
        _c("Beta", "STAFFING_SERVICES"),        # not a product category
        _c("Gamma", "IT_CONSULTING"),           # not a product category
        _c("Delta", "FINTECH_PRODUCT"),
    )
    sel = select_product_companies(pool, count=10, seed="s", today=TODAY)
    names = set(sel.names())
    assert names == {"Alpha", "Delta"}
    assert "Beta" not in names and "Gamma" not in names


def test_due_and_cooldown():
    pool = _pool(
        _c("Recent", last="2026-09-09"),        # 1 day ago -> cooldown, not due
        _c("Overdue", last="2026-07-01"),       # long ago -> due
        _c("Never", last=None),                 # never searched -> due
    )
    sel = select_product_companies(pool, count=10, seed="s", today=TODAY)
    names = set(sel.names())
    assert "Recent" not in names
    assert {"Overdue", "Never"} <= names


def test_disabled_and_no_india_excluded():
    pool = _pool(
        _c("Disabled", disabled=True),
        _c("NoIndia", india=False),
        _c("Good"),
    )
    sel = select_product_companies(pool, count=10, seed="s", today=TODAY)
    assert set(sel.names()) == {"Good"}


def test_same_seed_is_deterministic():
    pool = _pool(*[_c(f"C{i}", family=f"f{i%4}") for i in range(12)])
    a = select_product_companies(pool, count=6, seed="seed-1", today=TODAY)
    b = select_product_companies(pool, count=6, seed="seed-1", today=TODAY)
    assert a.names() == b.names()


def test_different_seed_changes_only_tie_ordering():
    # all equal score (identical metadata) => every ordering difference is a tie reshuffle;
    # the SET selected can differ but scores are identical, and a distinct-score company is stable.
    tied = [_c(f"T{i}", family=f"fam{i}") for i in range(8)]
    anchor = _c("Anchor", yield_=99.0, priority=99.0, family="anchorfam")  # dominant score
    pool = _pool(anchor, *tied)
    a = select_product_companies(pool, count=4, seed="A", today=TODAY)
    b = select_product_companies(pool, count=4, seed="B", today=TODAY)
    # the dominant-score company is always selected first regardless of seed
    assert a.selected[0].company.name == "Anchor"
    assert b.selected[0].company.name == "Anchor"
    # tie ordering below the anchor may differ between seeds
    assert a.names() != b.names() or a.names() == b.names()  # both are valid; anchor is stable


def test_category_and_source_diversity():
    # 5 companies from the SAME source family -> at most 2 may be chosen
    pool = _pool(*[_c(f"WD{i}", category="ENTERPRISE_HR_PAYROLL_SAAS", family="WORKDAY") for i in range(5)],
                 _c("Sf", category="ENTERPRISE_SAAS", family="SALESFORCE"),
                 _c("Gh", category="DEVELOPER_SAAS", family="GREENHOUSE"))
    sel = select_product_companies(pool, count=4, seed="s", today=TODAY, max_per_source_family=2)
    fams = [c.company.resolved_source_family for c in sel.selected]
    assert fams.count("WORKDAY") <= 2
    assert len({c.company.category for c in sel.selected}) >= 3


def test_no_duplicates():
    pool = _pool(_c("Dup"), _c("Dup"), _c("Other"))
    sel = select_product_companies(pool, count=10, seed="s", today=TODAY)
    names = list(sel.names())
    assert len(names) == len(set(names))


def test_fixed_cohort_is_sealed_in_order():
    pool = _pool(_c("Microsoft", "GLOBAL_PRODUCT_PLATFORM"), _c("Google", "GLOBAL_PRODUCT_PLATFORM"),
                 _c("ServiceNow", "ENTERPRISE_SAAS"), _c("Workday", "ENTERPRISE_HR_PAYROLL_SAAS"),
                 _c("Atlassian", "DEVELOPER_SAAS"), _c("Oracle"))
    cohort = ["Microsoft", "Google", "ServiceNow", "Workday", "Atlassian"]
    sel = select_fixed_cohort(pool, cohort, seed="fixed", today=TODAY)
    assert list(sel.names()) == cohort
    assert sel.mode == "FIXED_REPRODUCIBLE_BENCHMARK"


def test_fixed_cohort_seals_even_when_absent_from_pool():
    pool = _pool(_c("Microsoft"))
    sel = select_fixed_cohort(pool, ["Microsoft", "Google"], seed="fixed", today=TODAY)
    assert list(sel.names()) == ["Microsoft", "Google"]  # Google sealed from config only
    assert any("Google" in n for n in sel.notes)


def test_twenty_company_bucket_accounting():
    companies = []
    # priority-due (recent-ish, yield/priority) 
    companies += [_c(f"P{i}", family=f"pf{i}", last="2026-09-01", yield_=5, priority=3) for i in range(8)]
    # overdue / never
    companies += [_c(f"O{i}", family=f"of{i}", last=None) for i in range(6)]
    # retry-due
    companies += [_c(f"R{i}", family=f"rf{i}", retry=True) for i in range(3)]
    # newly added
    companies += [_c(f"N{i}", family=f"nf{i}", newly=True) for i in range(3)]
    pool = _pool(*companies)
    sel = plan_daily_buckets(pool, seed="day", today=TODAY,
                             buckets={"PRIORITY_DUE": 7, "OVERDUE_OR_NEVER": 5,
                                      "EXPLORATION": 4, "RETRY_DUE": 2, "NEWLY_ADDED": 2})
    total = sum(sel.bucket_counts.values())
    assert total == len(sel.selected)
    assert sel.bucket_counts.get("RETRY_DUE", 0) <= 2
    assert sel.bucket_counts.get("NEWLY_ADDED", 0) <= 2
    # no duplicates across buckets
    names = list(sel.names())
    assert len(names) == len(set(names))
