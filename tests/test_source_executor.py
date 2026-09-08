"""Phase 1B.1 — shared rate-limited executor integration (build spec 10 / P0-13)."""

from __future__ import annotations

import pytest

from atlas.models import ErrorCategory
from atlas.sources.executor import RateLimitedExecutor
from atlas.sources.rate_limit import ManualClock, RateLimiter, RatePolicy
from atlas.sources.models import Capability, SearchRequest
from atlas.sources.testing.fake import FakeAdapter, FakeScenario, make_fake_instance
from atlas.sources.worker import SourceSearchWorker, SourceTask
from atlas.models import TaskStatus

pytestmark = pytest.mark.unit


def _adapter(instance_id="inst", **scenario):
    inst = make_fake_instance(instance_id, **scenario)
    return FakeAdapter(inst)


def test_executor_enforces_min_interval_with_virtual_clock():
    clock = ManualClock()
    limiter = RateLimiter(RatePolicy(min_interval_seconds=5.0), clock=clock.time, sleeper=clock.sleep)
    ex = RateLimitedExecutor(limiter)
    a = _adapter(scenario="results", result_count=1)
    ex.run_search(a, SearchRequest(query="x"))
    t_after_first = clock.time()
    ex.run_search(a, SearchRequest(query="x"))
    # The second call must have waited the full min interval (deterministic).
    assert clock.time() - t_after_first >= 5.0


def test_executor_honors_retry_after_on_429():
    clock = ManualClock()
    limiter = RateLimiter(RatePolicy(min_interval_seconds=0.0), clock=clock.time, sleeper=clock.sleep)
    ex = RateLimitedExecutor(limiter)
    a = _adapter(scenario="error", error_category="HTTP_429", retry_after=30.0)
    with pytest.raises(Exception):
        ex.run_search(a, SearchRequest(query="x"))
    # not-before advanced by Retry-After seconds
    assert limiter.next_available("inst") >= clock.time() + 30.0


def test_executor_adaptive_backoff_without_retry_after():
    clock = ManualClock()
    limiter = RateLimiter(
        RatePolicy(min_interval_seconds=0.0, backoff_initial_seconds=2.0), clock=clock.time, sleeper=clock.sleep
    )
    ex = RateLimitedExecutor(limiter)
    a = _adapter(scenario="error", error_category="HTTP_429")
    with pytest.raises(Exception):
        ex.run_search(a, SearchRequest(query="x"))
    assert limiter.next_available("inst") >= clock.time() + 2.0


def test_executor_releases_slot_even_on_error():
    limiter = RateLimiter(RatePolicy(max_concurrency=1))
    ex = RateLimitedExecutor(limiter)
    a = _adapter(scenario="error", error_category="HTTP_5XX")
    with pytest.raises(Exception):
        ex.run_search(a, SearchRequest(query="x"))
    # slot released in finally -> a subsequent search can still acquire it
    assert limiter.active_count("inst") == 0


def test_mixed_source_concurrency_slots_are_per_key():
    limiter = RateLimiter(RatePolicy(max_concurrency=1))
    # two different sources each get their own slot
    assert limiter.acquire_slot("src-a") is True
    assert limiter.acquire_slot("src-b") is True
    # a second concurrent acquire on the same key is refused
    assert limiter.acquire_slot("src-a") is False
    limiter.release_slot("src-a")
    assert limiter.acquire_slot("src-a") is True


def test_worker_uses_executor_for_search():
    clock = ManualClock()
    limiter = RateLimiter(RatePolicy(min_interval_seconds=1.0), clock=clock.time, sleeper=clock.sleep)
    ex = RateLimitedExecutor(limiter)
    a = _adapter("wk", scenario="results", result_count=2)
    task = SourceTask(item_id="i1", instance_id="wk", request=SearchRequest(query="java", limit=10))
    worker = SourceSearchWorker({"wk": a}, {"i1": task}, executor=ex)
    outcome = worker.attempt("i1", 1)
    assert outcome.status == TaskStatus.SUCCESS
    assert outcome.payload["result_count"] == 2
