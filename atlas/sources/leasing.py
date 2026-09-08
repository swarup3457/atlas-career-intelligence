"""Atlas atomic coverage-task leasing (Phase 1C-A, build spec 7).

A thin, clock-injectable façade over the atomic lease operations on
:class:`atlas.persistence.sqlite.StateStore`. The exact coverage child is the
unique unit of work; a claim is atomic (``BEGIN IMMEDIATE`` in the store); a
second worker cannot claim an unexpired lease; an expired lease is reclaimable;
a long task can heartbeat; a terminal task can never be leased; duplicate
delivery is harmless; every transition is auditable; timestamps are UTC only.

Each :class:`LeaseManager` is bound to ONE StateStore connection, so a parallel
worker constructs its own — leases coordinate across connections/processes via
SQLite's reserved write lock, never by sharing a connection across threads. The
clock is injected as a callable returning an aware UTC ``datetime`` so tests use
a deterministic virtual clock and never sleep.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Callable, Optional

ClockFn = Callable[[], datetime.datetime]


def system_utc_clock() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt: datetime.datetime) -> str:
    """Fixed-width UTC ISO-8601 (always 6-digit microseconds) so lease-expiry
    string comparisons in SQL are correct."""
    return dt.astimezone(datetime.timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class Lease:
    coverage_id: str
    run_id: str
    worker_id: str
    attempt: int
    version: int
    lease_expires_at: str


class LeaseManager:
    """Clock-injectable wrapper over the store's atomic lease operations."""

    def __init__(
        self,
        store,
        run_id: str,
        *,
        ttl_seconds: float = 120.0,
        clock: ClockFn = system_utc_clock,
    ):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        self.store = store
        self.run_id = run_id
        self.ttl_seconds = float(ttl_seconds)
        self.clock = clock

    def _now_and_expiry(self) -> tuple[str, str]:
        now = self.clock()
        expiry = now + datetime.timedelta(seconds=self.ttl_seconds)
        return _iso(now), _iso(expiry)

    # -- setup --------------------------------------------------------------
    def ensure(
        self,
        coverage_id: str,
        *,
        company: Optional[str] = None,
        source_instance: Optional[str] = None,
        tenant: Optional[str] = None,
        terminal: bool = False,
    ) -> bool:
        return self.store.ensure_coverage_lease(
            coverage_id, self.run_id, company=company, source_instance=source_instance,
            tenant=tenant, terminal=terminal, now=_iso(self.clock()),
        )

    # -- claim --------------------------------------------------------------
    def acquire(self, coverage_id: str, worker_id: str) -> Optional[Lease]:
        now, expiry = self._now_and_expiry()
        row = self.store.acquire_coverage_lease(
            coverage_id, self.run_id, worker_id, now=now, lease_expires_at=expiry
        )
        return self._to_lease(row)

    def acquire_next(self, worker_id: str, *, eligible: Optional[list[str]] = None) -> Optional[Lease]:
        now, expiry = self._now_and_expiry()
        row = self.store.acquire_next_coverage_lease(
            self.run_id, worker_id, now=now, lease_expires_at=expiry, eligible=eligible
        )
        return self._to_lease(row)

    def heartbeat(self, coverage_id: str, worker_id: str) -> bool:
        now, expiry = self._now_and_expiry()
        return self.store.heartbeat_coverage_lease(
            coverage_id, self.run_id, worker_id, now=now, lease_expires_at=expiry
        )

    def complete(self, coverage_id: str, *, worker_id: Optional[str] = None, terminal_status: str = "DONE") -> None:
        self.store.complete_coverage_lease(
            coverage_id, self.run_id, worker_id=worker_id, now=_iso(self.clock()),
            terminal_status=terminal_status,
        )

    def release(self, coverage_id: str, *, worker_id: Optional[str] = None, requeue: bool = True) -> None:
        self.store.release_coverage_lease(
            coverage_id, self.run_id, worker_id=worker_id, now=_iso(self.clock()), requeue=requeue
        )

    # -- introspection ------------------------------------------------------
    def active_count(self) -> int:
        return self.store.count_active_coverage_leases(self.run_id, now=_iso(self.clock()))

    def _to_lease(self, row: Optional[dict]) -> Optional[Lease]:
        if not row:
            return None
        return Lease(
            coverage_id=row["coverage_id"], run_id=row["run_id"], worker_id=row["worker_id"],
            attempt=int(row["attempt"]), version=int(row["version"]),
            lease_expires_at=row["lease_expires_at"],
        )


class ManualUTCClock:
    """A deterministic virtual UTC clock for lease tests. ``advance`` moves time
    forward; nothing sleeps."""

    def __init__(self, start: Optional[datetime.datetime] = None):
        self._t = start or datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        if self._t.tzinfo is None:
            self._t = self._t.replace(tzinfo=datetime.timezone.utc)

    def __call__(self) -> datetime.datetime:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t = self._t + datetime.timedelta(seconds=float(seconds))


__all__ = ["Lease", "LeaseManager", "ManualUTCClock", "system_utc_clock", "ClockFn"]
