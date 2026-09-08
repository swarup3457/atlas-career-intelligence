"""Shared rate-limited adapter execution wrapper (Phase 1B.1, build spec 10 / P0-13).

Every production search attempt funnels through ONE common executor so rate
limiting is applied uniformly and centrally — never re-implemented inside each
adapter. The executor:

    * acquires a per-source concurrency slot and releases it in ``finally``;
    * enforces the per-source minimum interval / not-before pacing;
    * honors an explicit ``Retry-After`` from a 429 (:attr:`AdapterError.retry_after`);
    * applies an adaptive, bounded backoff on a 429 without an explicit delay;
    * notes a clean success so the adaptive backoff resets.

It performs EXACTLY ONE adapter call per invocation (one attempt == one call)
and NEVER retries — retry authority stays centralized in
:mod:`atlas.orchestration.retry`, applied by the governor. There is no random
"human-like" jitter; pacing is deterministic and virtual-clock testable.
"""

from __future__ import annotations

from typing import Optional

from atlas.models import ErrorCategory
from atlas.sources.adapter import AdapterError, SourceAdapter
from atlas.sources.models import SearchRequest, SearchResult
from atlas.sources.rate_limit import RateLimiter, RatePolicy


class RateLimitedExecutor:
    """Wraps one shared :class:`RateLimiter` and paces every adapter search.

    The rate key defaults to the adapter's instance id so pacing/concurrency
    are per source instance, but a caller may pass an explicit key (e.g. a
    shared tenant) when multiple instances must share a budget.
    """

    def __init__(self, limiter: Optional[RateLimiter] = None):
        self.limiter = limiter or RateLimiter(RatePolicy())

    def rate_key(self, adapter: SourceAdapter) -> str:
        return adapter.instance_id

    def run_search(
        self, adapter: SourceAdapter, request: SearchRequest, *, key: Optional[str] = None
    ) -> SearchResult:
        """Execute exactly one paced adapter search. On a 429 the executor
        records the rate-limit signal (honoring Retry-After when provided) and
        re-raises so the centralized retry policy decides what happens next —
        the executor never retries or swallows the error.

        The per-source concurrency slot is acquired in BLOCKING mode: under real
        parallel execution a worker waits for a free slot rather than exceeding
        the cap. Crucially, the adapter is called ONLY when a slot was actually
        acquired, and a slot is released ONLY when it was held — a failed
        acquisition never falls through to the adapter call nor corrupts the
        active-slot count (build spec 8)."""
        rate_key = key or self.rate_key(adapter)
        acquired = self.limiter.acquire_slot(rate_key, blocking=True)
        if not acquired:
            # Defensive: with blocking=True this is only reachable on a future
            # timeout variant. Never call the adapter without a held slot.
            raise AdapterError(
                ErrorCategory.SOURCE_UNAVAILABLE,
                f"could not acquire a concurrency slot for {rate_key!r}; adapter not called",
            )
        try:
            self.limiter.acquire(rate_key)  # pace: min interval + not-before
            try:
                result = adapter.search(request)
            except AdapterError as exc:
                if exc.category == ErrorCategory.HTTP_429:
                    if exc.retry_after is not None:
                        self.limiter.note_retry_after(rate_key, float(exc.retry_after))
                    else:
                        self.limiter.note_rate_limited(rate_key)
                raise
            self.limiter.note_success(rate_key)
            return result
        finally:
            self.limiter.release_slot(rate_key)


__all__ = ["RateLimitedExecutor"]
