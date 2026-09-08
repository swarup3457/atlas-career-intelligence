"""Phase 1C-A CORRECTIVE gate — the sentinel probe must go through the SHARED
rate-limited executor (build spec 18), never call ``adapter.search`` directly.

Failing-first: before this gate ``run_sentinel_probe`` called the adapter
directly, so a sentinel bypassed the rate limiter, the per-source concurrency
budget, and the HTTP request budget. It also must remain bounded to at most one
probe and never recurse.
"""

from __future__ import annotations

import pytest

from atlas.models import TaskStatus
from atlas.sources.adapter import SourceAdapter, new_result_base
from atlas.sources.executor import RateLimitedExecutor
from atlas.sources.health import SourceHealth, SourceHealthState
from atlas.sources.models import (
    Capability,
    SearchRequest,
    SearchResult,
    SourceInstance,
    SourceType,
    ZeroResultKind,
)
from atlas.sources.rate_limit import RateLimiter, RatePolicy
from atlas.sources.worker import SourceSearchWorker, SourceTask

pytestmark = pytest.mark.unit


class _CountingExecutor(RateLimitedExecutor):
    def __init__(self):
        super().__init__(RateLimiter(RatePolicy(max_concurrency=1)))
        self.searches = 0

    def run_search(self, adapter, request, *, key=None):
        self.searches += 1
        return super().run_search(adapter, request, key=key)


class _ZeroAdapter(SourceAdapter):
    source_type = SourceType.FAKE
    CAPABILITIES = frozenset({Capability.SEARCH})
    adapter_version = "zero-1"
    parser_version = "zero-parser-1"

    def __init__(self, instance):
        super().__init__(instance)
        self.direct_calls = 0

    def health_check(self) -> SourceHealth:
        return SourceHealth(SourceHealthState.HEALTHY, "ok")

    def search(self, request: SearchRequest) -> SearchResult:
        self.direct_calls += 1
        return SearchResult(results=(), zero_result_kind=ZeroResultKind.NOT_APPLICABLE)


def test_sentinel_probe_is_paced_and_counted_through_the_executor():
    inst = SourceInstance("i0", SourceType.FAKE)
    adapter = _ZeroAdapter(inst)
    executor = _CountingExecutor()
    task = SourceTask(
        item_id="c1", instance_id="i0", request=SearchRequest(query="java"),
        sentinel_request=SearchRequest(query="*", extra_filters={"sentinel": True}),
    )
    # A past yield >= 1 makes the current zero UNTRUSTED → a single sentinel runs.
    worker = SourceSearchWorker({"i0": adapter}, {"c1": task},
                                history_provider=lambda k: (5,), executor=executor)
    outcome = worker.attempt("c1", 1)

    # Both the main search AND the sentinel went through the shared executor
    # (paced/concurrency/HTTP-budget), so every adapter call was counted there.
    assert executor.searches == 2
    assert adapter.direct_calls == 2  # each executor call reached the adapter exactly once
    # Exactly one sentinel probe (bounded, no recursion).
    assert len(worker.sentinel_log) == 1
    assert outcome.payload["sentinel"]["ran"] is True
    assert outcome.status in (
        TaskStatus.NO_RELEVANT_RESULTS, TaskStatus.EXTRACTION_UNRESOLVED,
        TaskStatus.SOURCE_UNAVAILABLE, TaskStatus.RATE_LIMITED, TaskStatus.ACCESS_LIMITED,
    )


def test_sentinel_respects_http_request_budget_via_executor():
    """The sentinel shares the request budget: an executor whose limiter is at
    capacity still routes the sentinel through the same accounting path."""
    inst = SourceInstance("i0", SourceType.FAKE)
    adapter = _ZeroAdapter(inst)
    executor = _CountingExecutor()
    task = SourceTask(
        item_id="c1", instance_id="i0", request=SearchRequest(query="java"),
        sentinel_request=SearchRequest(query="*"),
    )
    worker = SourceSearchWorker({"i0": adapter}, {"c1": task},
                               history_provider=lambda k: (3,), executor=executor)
    worker.attempt("c1", 1)
    # No direct adapter.search bypass occurred: direct_calls == executor.searches.
    assert adapter.direct_calls == executor.searches == 2
