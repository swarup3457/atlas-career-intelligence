"""Phase 1A.5: company discovery worker tests."""

from __future__ import annotations

import pytest

from atlas.company.models import CompanyObservation, DiscoveryMethod
from atlas.company.registry import CompanyRegistry
from atlas.models import TaskStatus
from atlas.persistence.sqlite import StateStore
from atlas.workers.base import WorkerError
from atlas.workers.company import CompanyDiscoveryWorker

pytestmark = pytest.mark.unit

_C = DiscoveryMethod.CONFIRMED_IDENTITY


@pytest.fixture
def registry(tmp_path):
    with StateStore(tmp_path / "s.sqlite") as store:
        yield CompanyRegistry(store)


def test_success_registers_company_and_source(registry):
    plan = {"acme": CompanyObservation(name="Acme", official_domain="acme.com",
                                       careers_url="https://boards.greenhouse.io/acme", method=_C)}
    worker = CompanyDiscoveryWorker(registry, plan)
    outcome = worker.attempt("acme", 1)
    assert outcome.status == TaskStatus.SUCCESS
    assert outcome.payload["source_registered"] is True
    assert registry.store.count_companies() == 1


def test_malformed_observation_is_extraction_unresolved(registry):
    plan = {"bad": CompanyObservation(name="   ", method=_C)}
    worker = CompanyDiscoveryWorker(registry, plan)
    outcome = worker.attempt("bad", 1)
    assert outcome.status == TaskStatus.EXTRACTION_UNRESOLVED
    assert registry.store.count_companies() == 0


def test_transient_failure_raises_worker_error(registry):
    plan = {"x": CompanyObservation(name="Acme", official_domain="acme.com", method=_C)}
    worker = CompanyDiscoveryWorker(registry, plan, simulated_first_attempt_failures={"x"})
    with pytest.raises(WorkerError):
        worker.attempt("x", 1)
    # second attempt succeeds (worker does not own retry)
    assert worker.attempt("x", 2).status == TaskStatus.SUCCESS


def test_missing_plan_item_is_config_error(registry):
    worker = CompanyDiscoveryWorker(registry, {})
    with pytest.raises(WorkerError):
        worker.attempt("nope", 1)
