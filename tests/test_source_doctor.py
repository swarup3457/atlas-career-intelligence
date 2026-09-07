"""Phase 1A: doctor extension covers the source framework."""

from __future__ import annotations

import pytest

from atlas.health import FAIL, run_doctor

pytestmark = pytest.mark.integration


def test_doctor_reports_source_framework_checks():
    report = run_doctor()
    source_checks = [r for r in report.results if r.name.startswith("Sources:")]
    assert source_checks, "doctor must include Sources: checks"
    names = {r.name for r in source_checks}
    assert "Sources: registry + descriptors" in names
    assert "Sources: coverage/health schema" in names
    assert "Sources: evidence store" in names
    assert "Sources: adapter contract harness" in names
    assert all(r.status != FAIL for r in source_checks), report.render()
    assert report.overall != FAIL
