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
import threading
from dataclasses import dataclass
from typing import Callable, Optional, Union

from atlas.persistence.sqlite import LeaseMutationResult

ClockFn = Callable[[], datetime.datetime]


def system_utc_clock() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt: datetime.datetime) -> str:
    """Fixed-width UTC ISO-8601 (always 6-digit microseconds) so lease-expiry
    string comparisons in SQL are correct."""
    return dt.astimezone(datetime.timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class Lease:
    """Immutable lease token. Carries everything needed to FENCE a later
    mutation: only the current ``(run_id, coverage_id, worker_id, version)``
    owner may heartbeat/complete/release. ``version`` is the fencing token — it
    increments on every (re)acquire, so a stale worker's token can never mutate
    a lease another worker has reclaimed."""

    coverage_id: str
    run_id: str
    worker_id: str
    attempt: int
    version: int
    lease_expires_at: str


class LeaseManager:
    """Clock-injectable wrapper over the store's atomic, FENCED lease operations."""

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

    @staticmethod
    def _resolve(
        lease_or_id: Union["Lease", str], worker_id: Optional[str], version: Optional[int]
    ) -> tuple[str, Optional[str], Optional[int]]:
        if isinstance(lease_or_id, Lease):
            return lease_or_id.coverage_id, lease_or_id.worker_id, lease_or_id.version
        return lease_or_id, worker_id, version

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

    def heartbeat(
        self, lease_or_id: Union[Lease, str], worker_id: Optional[str] = None,
    ) -> LeaseMutationResult:
        cid, worker_id, version = self._resolve(lease_or_id, worker_id, None)
        now, expiry = self._now_and_expiry()
        return self.store.heartbeat_coverage_lease(
            cid, self.run_id, worker_id, version=version, now=now, lease_expires_at=expiry
        )

    def complete(
        self, lease_or_id: Union[Lease, str], *, worker_id: Optional[str] = None,
        version: Optional[int] = None, terminal_status: str = "DONE",
    ) -> LeaseMutationResult:
        cid, worker_id, version = self._resolve(lease_or_id, worker_id, version)
        return self.store.complete_coverage_lease(
            cid, self.run_id, worker_id=worker_id, version=version, now=_iso(self.clock()),
            terminal_status=terminal_status,
        )

    def release(
        self, lease_or_id: Union[Lease, str], *, worker_id: Optional[str] = None,
        version: Optional[int] = None, requeue: bool = True,
    ) -> LeaseMutationResult:
        cid, worker_id, version = self._resolve(lease_or_id, worker_id, version)
        return self.store.release_coverage_lease(
            cid, self.run_id, worker_id=worker_id, version=version, now=_iso(self.clock()), requeue=requeue
        )

    def reopen_human(
        self, coverage_id: str, *, reason: str = "", reference: Optional[str] = None
    ) -> LeaseMutationResult:
        """Explicit, authorized reopen of a BLOCKED_HUMAN child (build spec 9).
        Only callable from a deliberate resume-after-human path — NEVER from
        ordinary retry/resume."""
        return self.store.reopen_human_coverage_lease(
            coverage_id, self.run_id, now=_iso(self.clock()), reason=reason, reference=reference
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


class LeaseHeartbeat:
    """A bounded, periodic heartbeat for ONE owned lease (build spec 8).

    Starts after lease acquisition, renews at ``interval_seconds`` (default
    ttl/3) using its OWN StateStore connection (never sharing the child's
    connection across threads), stops in ``finally``, and surfaces a stale-token
    rejection to the owner via ``stale`` / ``on_stale`` — it never hides a
    reclaim. For deterministic virtual-clock tests, ``beat_once`` performs one
    synchronous heartbeat without a background thread."""

    def __init__(
        self,
        store_factory: Callable[[], object],
        run_id: str,
        lease: Lease,
        *,
        ttl_seconds: float,
        clock: ClockFn = system_utc_clock,
        interval_seconds: Optional[float] = None,
        on_stale: Optional[Callable[[LeaseMutationResult], None]] = None,
    ):
        self._store_factory = store_factory
        self.run_id = run_id
        self.lease = lease
        self.ttl_seconds = float(ttl_seconds)
        self.clock = clock
        self.interval_seconds = (
            float(interval_seconds) if interval_seconds is not None else max(self.ttl_seconds / 3.0, 0.001)
        )
        self._on_stale = on_stale
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.beats = 0
        self.stale = False
        self._store = None
        self._mgr: Optional[LeaseManager] = None

    def _ensure_mgr(self) -> LeaseManager:
        if self._mgr is None:
            self._store = self._store_factory()
            self._mgr = LeaseManager(self._store, self.run_id, ttl_seconds=self.ttl_seconds, clock=self.clock)
        return self._mgr

    def beat_once(self) -> LeaseMutationResult:
        mgr = self._ensure_mgr()
        res = mgr.heartbeat(self.lease)
        if res == LeaseMutationResult.APPLIED:
            self.beats += 1
        elif res in (LeaseMutationResult.STALE_TOKEN_REJECTED, LeaseMutationResult.NOT_FOUND):
            self.stale = True
            if self._on_stale is not None:
                self._on_stale(res)
        return res

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.beat_once()
            except Exception:  # noqa: BLE001 - a heartbeat error must never crash the worker
                pass
            if self.stale:
                break

    def start(self) -> "LeaseHeartbeat":
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name=f"lease-hb-{self.lease.coverage_id}"
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(self.ttl_seconds, 5.0))
            self._thread = None
        if self._store is not None:
            try:
                self._store.close()
            except Exception:  # noqa: BLE001
                pass
            self._store = None
            self._mgr = None

    def __enter__(self) -> "LeaseHeartbeat":
        return self.start()

    def __exit__(self, *exc) -> bool:
        self.stop()
        return False


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


__all__ = [
    "Lease",
    "LeaseManager",
    "LeaseHeartbeat",
    "LeaseMutationResult",
    "ManualUTCClock",
    "system_utc_clock",
    "ClockFn",
]
