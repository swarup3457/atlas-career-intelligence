"""Atlas process-safe single-run guard.

Ensures a scheduled Atlas production run never overlaps with another
already-active run (e.g. a 7 AM run still in progress when a later
invocation starts). Built on the same PID-aware PidLock used for the
browser profile lock (see atlas/utils/pidlock.py), so a crashed run's
lock is safely reclaimed while a genuinely live run's lock is never
touched.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from atlas.utils.pidlock import LockHeldError, PidLock

RUN_ALREADY_ACTIVE = "RUN_ALREADY_ACTIVE"
RUN_LOCK_ACQUIRED = "RUN_LOCK_ACQUIRED"

_RUN_LOCK_FILENAME = "atlas_run.lock"


@dataclass
class RunLockStatus:
    status: str  # RUN_ALREADY_ACTIVE | RUN_LOCK_ACQUIRED
    run_id: Optional[str] = None
    pid: Optional[int] = None
    started_at: Optional[str] = None


class RunLock:
    """One process-wide lock guarding "is an Atlas run currently active".

    Usage:
        run_lock = RunLock(state_dir)
        result = run_lock.try_acquire(run_id="2026-09-06T07:00")
        if result.status == RUN_ALREADY_ACTIVE:
            return result  # do NOT start a second run
        try:
            ... perform the run ...
        finally:
            run_lock.release()
    """

    def __init__(self, state_dir: Path, filename: str = _RUN_LOCK_FILENAME):
        self.state_dir = Path(state_dir)
        self._lock = PidLock(self.state_dir / filename)

    def try_acquire(self, run_id: str) -> RunLockStatus:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            info = self._lock.acquire(metadata={"run_id": run_id})
        except LockHeldError as exc:
            return RunLockStatus(
                status=RUN_ALREADY_ACTIVE,
                run_id=exc.info.metadata.get("run_id"),
                pid=exc.info.pid,
                started_at=exc.info.acquired_at,
            )
        return RunLockStatus(
            status=RUN_LOCK_ACQUIRED,
            run_id=run_id,
            pid=info.pid,
            started_at=info.acquired_at,
        )

    def release(self) -> None:
        self._lock.release()

    def current_holder(self) -> Optional[RunLockStatus]:
        """Read-only: report who (if anyone) currently holds the run lock,
        without attempting to acquire or reclaim it."""
        info = self._lock.read_info()
        if info is None:
            return None
        return RunLockStatus(
            status=RUN_ALREADY_ACTIVE,
            run_id=info.metadata.get("run_id"),
            pid=info.pid,
            started_at=info.acquired_at,
        )

    def __enter__(self) -> "RunLock":
        result = self.try_acquire(run_id=datetime.datetime.now(datetime.timezone.utc).isoformat())
        if result.status == RUN_ALREADY_ACTIVE:
            raise RuntimeError(
                f"{RUN_ALREADY_ACTIVE}: another Atlas run (PID {result.pid}, "
                f"run_id={result.run_id}, started_at={result.started_at}) is already active."
            )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
