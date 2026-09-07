"""Phase 1A: per-source rate limiter tests (deterministic virtual clock)."""

from __future__ import annotations

import pytest

from atlas.sources.rate_limit import ManualClock, RateLimitConfigError, RateLimiter, RatePolicy

pytestmark = pytest.mark.unit


def _limiter(policy: RatePolicy) -> tuple[RateLimiter, ManualClock]:
    clock = ManualClock()
    return RateLimiter(policy, clock=clock.time, sleeper=clock.sleep), clock


def test_min_interval_enforced():
    rl, clock = _limiter(RatePolicy(min_interval_seconds=5))
    assert rl.acquire("k") == 0.0
    waited = rl.acquire("k")
    assert waited == 5.0
    assert clock.time() == 5.0


def test_retry_after_respected():
    rl, clock = _limiter(RatePolicy(min_interval_seconds=0))
    rl.acquire("k")
    rl.note_retry_after("k", 10)
    waited = rl.acquire("k")
    assert waited == 10.0


def test_adaptive_backoff_grows_and_resets():
    rl, _ = _limiter(RatePolicy(min_interval_seconds=0, backoff_initial_seconds=1, backoff_multiplier=2, backoff_max_seconds=8))
    assert rl.note_rate_limited("k") == 1
    assert rl.note_rate_limited("k") == 2
    assert rl.note_rate_limited("k") == 4
    assert rl.note_rate_limited("k") == 8
    assert rl.note_rate_limited("k") == 8  # capped
    rl.note_success("k")
    assert rl.note_rate_limited("k") == 1  # reset


def test_concurrency_cap():
    rl, _ = _limiter(RatePolicy(max_concurrency=2))
    assert rl.acquire_slot("k") is True
    assert rl.acquire_slot("k") is True
    assert rl.acquire_slot("k") is False
    assert rl.at_capacity("k") is True
    rl.release_slot("k")
    assert rl.acquire_slot("k") is True


def test_invalid_policy_rejected():
    with pytest.raises(RateLimitConfigError):
        RatePolicy(min_interval_seconds=-1).validate()
    with pytest.raises(RateLimitConfigError):
        RatePolicy(max_concurrency=0).validate()
    with pytest.raises(RateLimitConfigError):
        RatePolicy(backoff_multiplier=0.5).validate()


def test_retry_after_negative_rejected():
    rl, _ = _limiter(RatePolicy())
    with pytest.raises(RateLimitConfigError):
        rl.note_retry_after("k", -1)
