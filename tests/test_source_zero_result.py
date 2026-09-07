"""Phase 1A: zero-result safety + bounded sentinel probe tests."""

from __future__ import annotations

import pytest

from atlas.models import ErrorCategory
from atlas.sources.health import SourceHealthState
from atlas.sources.models import DiscoveryResult, SearchRequest, SearchResult, SourceType, ZeroResultKind
from atlas.sources.testing.fake import FakeAdapter, FakeScenario, make_fake_instance
from atlas.sources.zero_result import (
    assess_search,
    health_state_for_category,
    run_sentinel_probe,
    should_run_sentinel,
)

pytestmark = pytest.mark.unit

_SENTINEL_REQ = SearchRequest(query="*", extra_filters={"sentinel": True})


def _fake(sentinel="healthy"):
    return FakeAdapter(make_fake_instance("fk"), FakeScenario(kind="untrusted_zero", sentinel=sentinel))


def test_results_present_is_not_applicable():
    r = SearchResult(results=(DiscoveryResult(source_type=SourceType.FAKE, source_instance="i", title="T"),))
    assert assess_search(r) == ZeroResultKind.NOT_APPLICABLE


def test_trusted_zero():
    r = SearchResult(results=(), zero_result_kind=ZeroResultKind.TRUSTED_ZERO)
    assert assess_search(r) == ZeroResultKind.TRUSTED_ZERO


def test_untrusted_zero_from_history():
    r = SearchResult(results=(), zero_result_kind=ZeroResultKind.TRUSTED_ZERO)
    assert assess_search(r, historical_yields=(40, 38, 45)) == ZeroResultKind.UNTRUSTED_ZERO


def test_extraction_unresolved_from_parse_findings():
    r = SearchResult(results=(), zero_result_kind=ZeroResultKind.EXTRACTION_UNRESOLVED,
                     parse_findings=("bad card",))
    assert assess_search(r) == ZeroResultKind.EXTRACTION_UNRESOLVED


def test_should_run_sentinel_only_untrusted():
    assert should_run_sentinel(ZeroResultKind.UNTRUSTED_ZERO) is True
    assert should_run_sentinel(ZeroResultKind.TRUSTED_ZERO) is False


def test_sentinel_healthy_means_trusted_zero():
    adapter = _fake("healthy")
    outcome = run_sentinel_probe(adapter, _SENTINEL_REQ)
    assert outcome.ran is True
    assert outcome.kind == ZeroResultKind.TRUSTED_ZERO
    assert outcome.health.state == SourceHealthState.HEALTHY


def test_sentinel_drift_suspected():
    adapter = _fake("drift")
    outcome = run_sentinel_probe(adapter, _SENTINEL_REQ)
    assert outcome.health.state == SourceHealthState.SELECTOR_DRIFT_SUSPECTED
    assert outcome.kind == ZeroResultKind.UNTRUSTED_ZERO


def test_sentinel_error_classifies_health():
    adapter = FakeAdapter(
        make_fake_instance("fk"),
        FakeScenario(kind="untrusted_zero", sentinel="error", sentinel_error_category=ErrorCategory.HTTP_429),
    )
    outcome = run_sentinel_probe(adapter, _SENTINEL_REQ)
    assert outcome.ran is True
    assert outcome.health.state == SourceHealthState.RATE_LIMITED


def test_sentinel_is_bounded_single_probe():
    adapter = _fake("healthy")
    run_sentinel_probe(adapter, _SENTINEL_REQ)
    assert adapter._search_calls == 1  # exactly one probe, never a loop


def test_health_state_for_category_mapping():
    assert health_state_for_category(ErrorCategory.HTTP_5XX) == SourceHealthState.SOURCE_UNAVAILABLE
    assert health_state_for_category(ErrorCategory.PARSE_FAILURE) == SourceHealthState.SELECTOR_DRIFT_SUSPECTED
    assert health_state_for_category(ErrorCategory.LOGIN_WALL) == SourceHealthState.AUTH_REQUIRED
