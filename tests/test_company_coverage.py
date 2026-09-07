"""Phase 1A.5: company↔source coverage linkage tests."""

from __future__ import annotations

import pytest

from atlas.company.coverage import company_coverage_tasks, company_source_coverage_id
from atlas.company.discovery import mark_source_replaced, register_employer
from atlas.company.models import CompanyObservation, DiscoveryMethod
from atlas.company.registry import CompanyRegistry
from atlas.persistence.sqlite import StateStore
from atlas.sources.coverage import CoverageStatus

pytestmark = pytest.mark.unit

_C = DiscoveryMethod.CONFIRMED_IDENTITY


def test_coverage_tasks_one_per_current_source(tmp_path):
    with StateStore(tmp_path / "s.sqlite") as store:
        reg = CompanyRegistry(store)
        register_employer(reg, CompanyObservation(name="MultiCo", official_domain="multico.com",
                          careers_url="https://multico.wd1.myworkdayjobs.com/Global", method=_C))
        r2 = register_employer(reg, CompanyObservation(name="MultiCo", official_domain="multico.com",
                          careers_url="https://boards.greenhouse.io/multicoindia", method=_C))
        cid = r2.company.company_id
        tasks = company_coverage_tasks(reg, cid, lane="delta")
        assert len(tasks) == 2
        assert all(t.company == "MultiCo" and t.lane == "delta" for t in tasks)
        assert all(t.status == CoverageStatus.NOT_ATTEMPTED for t in tasks)
        assert {t.coverage_id for t in tasks} == {
            company_source_coverage_id(cid, r.instance_id) for r in reg.list_relationships(cid)
        }


def test_coverage_excludes_noncurrent_by_default(tmp_path):
    with StateStore(tmp_path / "s.sqlite") as store:
        reg = CompanyRegistry(store)
        old = register_employer(reg, CompanyObservation(name="OldCo", official_domain="oldco.com",
                          careers_url="https://oldco.taleo.net/careersection/x", method=_C))
        register_employer(reg, CompanyObservation(name="OldCo", official_domain="oldco.com",
                          careers_url="https://oldco.wd1.myworkdayjobs.com/New", method=_C))
        mark_source_replaced(reg, old.company.company_id, old.source_instance.instance_id)
        cid = old.company.company_id
        assert len(company_coverage_tasks(reg, cid)) == 1  # replaced one excluded
        assert len(company_coverage_tasks(reg, cid, include_noncurrent=True)) == 2
