"""Phase 1A: reusable adapter-contract harness applied to Fake + Fixture."""

from __future__ import annotations

import pytest

from atlas.models import ErrorCategory
from atlas.sources.testing.contract import run_contract_checks
from atlas.sources.testing.fake import FakeAdapter, FakeScenario, make_fake_instance
from atlas.sources.testing.fixture import FixtureAdapter, make_fixture_instance

pytestmark = pytest.mark.unit

REQUIRED = {
    "registration_identity", "typed_result_schema", "query_validation",
    "pagination_semantics", "recency_semantics", "one_bad_item_isolation",
    "map_429", "map_5xx", "map_timeout", "not_found_detail",
    "zero_result_trusted", "zero_result_untrusted", "sentinel_behavior",
    "selector_drift", "closed_state", "unicode", "health_check",
    "no_secret_evidence",
}

_VALID = {"id": "1", "title": "Engineer", "company": "Acme", "location": "Mumbai",
          "status": "open", "work_mode": "remote"}


def _fake_factory(scenario):
    if scenario == "results":
        sc = FakeScenario(kind="results", result_count=3)
    elif scenario == "zero":
        sc = FakeScenario(kind="zero")
    elif scenario == "untrusted_zero":
        sc = FakeScenario(kind="untrusted_zero", sentinel="healthy")
    elif scenario == "selector_drift":
        sc = FakeScenario(kind="untrusted_zero", sentinel="drift")
    elif scenario.startswith("error:"):
        sc = FakeScenario(kind="error", error_category=ErrorCategory(scenario.split(":", 1)[1]))
    else:
        return None
    return FakeAdapter(make_fake_instance("fk"), sc)


def _fixture_factory(scenario):
    if scenario == "results":
        items = [_VALID, dict(_VALID, id="1b", title="Engineer II")]
    elif scenario == "zero":
        items = []
    elif scenario == "untrusted_zero":
        return FixtureAdapter(make_fixture_instance("fx", []), untrusted_zero=True)
    elif scenario == "malformed":
        items = [_VALID, {"nope": "x"}, dict(_VALID, id="1c")]
    elif scenario == "closed":
        items = [_VALID, {"id": "2", "title": "Old", "company": "Acme", "location": "Pune", "status": "closed"}]
    elif scenario == "unicode":
        items = [{"id": "3", "title": "Se\u00f1or Caf\u00e9 \u8f6f\u4ef6 Engineer",
                  "company": "Acme", "location": "Delhi", "status": "open"}]
    elif scenario == "notfound":
        items = [_VALID]
    elif scenario == "selector_drift":
        items = [{"garbage": 1}, {"junk": 2}]
    else:
        return None
    return FixtureAdapter(make_fixture_instance("fx-" + scenario, items))


def test_fake_adapter_passes_contract():
    report = run_contract_checks(_fake_factory)
    assert report.ok, report.render()


def test_fixture_adapter_passes_contract():
    report = run_contract_checks(_fixture_factory)
    assert report.ok, report.render()


def test_union_covers_all_required_categories():
    fake = run_contract_checks(_fake_factory)
    fixture = run_contract_checks(_fixture_factory)
    passed = {n for n in REQUIRED if fake.passed(n) or fixture.passed(n)}
    missing = REQUIRED - passed
    assert not missing, f"categories never PASSED by either adapter: {sorted(missing)}"
