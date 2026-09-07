"""Phase 1A.5: doctor covers company/source registry integrity."""

from __future__ import annotations

import pytest

from atlas.health import FAIL, run_doctor

pytestmark = pytest.mark.integration


def test_doctor_reports_company_registry_checks():
    report = run_doctor()
    checks = [r for r in report.results if r.name.startswith("Company:")]
    assert checks, "doctor must include Company: checks"
    names = {r.name for r in checks}
    assert "Company: registry schema" in names
    assert "Company: relationship integrity" in names
    assert "Company: ATS fingerprint framework" in names
    assert all(r.status != FAIL for r in checks), report.render()
    assert report.overall != FAIL
