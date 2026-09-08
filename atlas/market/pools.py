"""Bounded worker pools with concurrency classes (Phase 1D §9).

One central dispatcher enforces per-class concurrency caps on top of the shared
:class:`atlas.runtime.concurrency.BoundedExecutor`:

    OFFICIAL_HTTP=4, PUBLIC_BROWSER_ANONYMOUS=2, PORTAL_BROWSER_AUTHENTICATED=1,
    REASONING=2, REPORT_WRITER=1.

A task's :class:`atlas.sources.models.ConcurrencyClass` maps to exactly one pool.
The authenticated-portal pool cap of 1 guarantees a single owner of the one
authenticated profile — two owners of one profile is structurally impossible. The
dispatcher records the maximum concurrency actually observed per pool so a live
run can prove the caps were respected, and there is only ever ONE report writer.

This is scheduling plumbing only — it owns no persistence and no completion
authority (the LangGraph governor + deterministic campaign loop own those).
"""

from __future__ import annotations

import threading
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from atlas.runtime.concurrency import BoundedExecutor
from atlas.sources.models import ConcurrencyClass

# Pool names + default caps (build spec 9).
OFFICIAL_HTTP = "OFFICIAL_HTTP"
PUBLIC_BROWSER_ANONYMOUS = "PUBLIC_BROWSER_ANONYMOUS"
PORTAL_BROWSER_AUTHENTICATED = "PORTAL_BROWSER_AUTHENTICATED"
REASONING = "REASONING"
REPORT_WRITER = "REPORT_WRITER"

DEFAULT_POOL_CAPS: dict[str, int] = {
    OFFICIAL_HTTP: 4,
    PUBLIC_BROWSER_ANONYMOUS: 2,
    PORTAL_BROWSER_AUTHENTICATED: 1,
    REASONING: 2,
    REPORT_WRITER: 1,
}

# A source/adapter concurrency class maps to exactly one network pool.
CLASS_TO_POOL: dict[ConcurrencyClass, str] = {
    ConcurrencyClass.HTTP: OFFICIAL_HTTP,
    ConcurrencyClass.BROWSER_ANONYMOUS: PUBLIC_BROWSER_ANONYMOUS,
    ConcurrencyClass.BROWSER_AUTHENTICATED: PORTAL_BROWSER_AUTHENTICATED,
    ConcurrencyClass.HUMAN_INTERVENTION: PORTAL_BROWSER_AUTHENTICATED,
}


def pool_for_class(concurrency_class: ConcurrencyClass) -> str:
    return CLASS_TO_POOL.get(concurrency_class, OFFICIAL_HTTP)


@dataclass
class PoolTask:
    task_id: str
    pool: str
    fn: Callable[[], Any]


@dataclass
class PoolRunResult:
    results: dict[str, Any] = field(default_factory=dict)
    errors: dict[str, BaseException] = field(default_factory=dict)
    max_observed: dict[str, int] = field(default_factory=dict)
    profile_owner_max: int = 0

    def ok(self, task_id: str) -> bool:
        return task_id in self.results and task_id not in self.errors


class MarketPoolDispatcher:
    """Runs a batch of pool tasks under per-class concurrency caps."""

    def __init__(self, caps: Optional[dict[str, int]] = None):
        self.caps = dict(DEFAULT_POOL_CAPS)
        if caps:
            self.caps.update(caps)
        self._executors: dict[str, BoundedExecutor] = {}
        self._auth_lock = threading.Lock()
        self._auth_owners = 0
        self._auth_owner_max = 0

    def _executor(self, pool: str) -> BoundedExecutor:
        if pool not in self._executors:
            self._executors[pool] = BoundedExecutor(max_parallel_tasks=max(1, self.caps.get(pool, 1)))
        return self._executors[pool]

    def _wrap_auth(self, pool: str, fn: Callable[[], Any]) -> Callable[[], Any]:
        """For the authenticated-portal pool, additionally track that at most ONE
        owner of the authenticated profile is ever active (defense in depth over
        the cap of 1)."""
        if pool != PORTAL_BROWSER_AUTHENTICATED:
            return fn

        def _inner():
            with self._auth_lock:
                self._auth_owners += 1
                self._auth_owner_max = max(self._auth_owner_max, self._auth_owners)
                owners = self._auth_owners
            if owners > 1:
                with self._auth_lock:
                    self._auth_owners -= 1
                raise RuntimeError("refusing a second concurrent authenticated-profile owner")
            try:
                return fn()
            finally:
                with self._auth_lock:
                    self._auth_owners -= 1

        return _inner

    def run(self, tasks: list[PoolTask]) -> PoolRunResult:
        """Submit all tasks bounded by their pool caps and wait for completion.
        Returns per-task results/errors and the max concurrency observed per
        pool."""
        result = PoolRunResult()
        futures: dict[str, tuple[str, Future]] = {}
        # Submit grouped by pool so each pool's BoundedExecutor bounds its class.
        by_pool: dict[str, list[PoolTask]] = {}
        for t in tasks:
            by_pool.setdefault(t.pool, []).append(t)
        for pool, plist in by_pool.items():
            ex = self._executor(pool)
            for t in plist:
                fut = ex.submit(t.task_id, self._wrap_auth(pool, t.fn))
                futures[t.task_id] = (pool, fut)
        for task_id, (pool, fut) in futures.items():
            try:
                result.results[task_id] = fut.result()
            except BaseException as exc:  # noqa: BLE001 - collect, never abort the batch
                result.errors[task_id] = exc
        for pool, ex in self._executors.items():
            result.max_observed[pool] = ex.max_observed_concurrency
        result.profile_owner_max = self._auth_owner_max
        return result

    def shutdown(self) -> None:
        for ex in self._executors.values():
            ex.shutdown(wait=True)
        self._executors.clear()

    def __enter__(self) -> "MarketPoolDispatcher":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.shutdown()


__all__ = [
    "OFFICIAL_HTTP",
    "PUBLIC_BROWSER_ANONYMOUS",
    "PORTAL_BROWSER_AUTHENTICATED",
    "REASONING",
    "REPORT_WRITER",
    "DEFAULT_POOL_CAPS",
    "CLASS_TO_POOL",
    "pool_for_class",
    "PoolTask",
    "PoolRunResult",
    "MarketPoolDispatcher",
]
