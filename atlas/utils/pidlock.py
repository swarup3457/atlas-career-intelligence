"""Generic, process-safe, PID-aware file lock.

Used by:
    - atlas.browser.manager.BrowserManager (one profile <-> one live Chrome
      process at a time)
    - atlas.orchestration.run_lock (one live Atlas scheduled run at a time)

Design goals (see PHASE 0.5 spec sections 4/5):
    - never delete a lock that is genuinely owned by a live process;
    - detect stale locks safely (owning process no longer exists);
    - record owning PID (and other small, non-secret metadata);
    - release cleanly on normal shutdown;
    - recover safely after a crashed Atlas process.

This is a cooperative, single-machine lock (a lock *file*, not an OS
kernel-level lock) - by design, since Atlas processes are the only
expected writers/readers of it. Liveness is verified via the owning PID
(see atlas.utils.procutil.is_pid_running), which is far more reliable than
a pure time-based staleness heuristic and is the primary mechanism used
here; a time-based staleness fallback is only used if the lock file is
unreadable/corrupt.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from atlas.utils.procutil import current_pid, is_pid_running


class LockHeldError(RuntimeError):
    """Raised when a PidLock is genuinely held by another live process."""

    def __init__(self, message: str, info: "LockInfo"):
        super().__init__(message)
        self.info = info


@dataclass
class LockInfo:
    pid: int
    hostname: str
    acquired_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "pid": self.pid,
                "hostname": self.hostname,
                "acquired_at": self.acquired_at,
                "metadata": self.metadata,
            }
        )

    @classmethod
    def from_json(cls, text: str) -> "LockInfo":
        data = json.loads(text)
        return cls(
            pid=int(data["pid"]),
            hostname=str(data.get("hostname", "")),
            acquired_at=str(data.get("acquired_at", "")),
            metadata=dict(data.get("metadata", {})),
        )


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class PidLock:
    """A single PID-aware lock file at `lock_path`.

    Corrupt/unreadable lock files fall back to a time-based staleness
    check (`corrupt_stale_after_seconds`) so a damaged lock file can never
    permanently wedge Atlas.
    """

    def __init__(self, lock_path: Path, corrupt_stale_after_seconds: float = 6 * 3600):
        self.lock_path = Path(lock_path)
        self.corrupt_stale_after_seconds = corrupt_stale_after_seconds
        self._held = False

    def read_info(self) -> Optional[LockInfo]:
        if not self.lock_path.exists():
            return None
        try:
            return LockInfo.from_json(self.lock_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return None

    def _is_stale(self, info: Optional[LockInfo]) -> bool:
        if info is None:
            # Unreadable/corrupt lock file - fall back to age-based staleness.
            try:
                age = time.time() - self.lock_path.stat().st_mtime
            except OSError:
                return True
            return age >= self.corrupt_stale_after_seconds
        # PID-based liveness is authoritative: a lock is stale iff its
        # owning process no longer exists, regardless of age.
        return not is_pid_running(info.pid)

    def acquire(self, metadata: Optional[dict[str, Any]] = None) -> LockInfo:
        """Acquire the lock, reclaiming a stale lock if necessary.

        Raises LockHeldError if a live process genuinely holds the lock.
        """
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

        existing = self.read_info()
        if existing is not None and not self._is_stale(existing):
            raise LockHeldError(
                f"Lock at {self.lock_path} is held by live PID {existing.pid} "
                f"(acquired_at={existing.acquired_at}).",
                info=existing,
            )
        if self.lock_path.exists() and (existing is None or self._is_stale(existing)):
            # Reclaim: the owning process is gone (or the file was corrupt
            # and old enough). Safe to remove and recreate.
            with contextlib.suppress(OSError):
                self.lock_path.unlink()

        info = LockInfo(
            pid=current_pid(),
            hostname=socket.gethostname(),
            acquired_at=_utcnow(),
            metadata=metadata or {},
        )
        # Atomic-ish create: write to a temp file then rename, avoiding a
        # window where a half-written lock file could be read by another
        # process as corrupt-but-not-stale.
        tmp_path = self.lock_path.with_suffix(self.lock_path.suffix + f".tmp{os.getpid()}")
        tmp_path.write_text(info.to_json(), encoding="utf-8")
        os.replace(tmp_path, self.lock_path)
        self._held = True
        return info

    def release(self) -> None:
        """Release the lock, but ONLY if it is still owned by this
        process (never remove a lock acquired by someone else)."""
        if not self._held:
            return
        info = self.read_info()
        if info is not None and info.pid != current_pid():
            # Someone else's lock (should not normally happen) - do not
            # touch it.
            self._held = False
            return
        with contextlib.suppress(OSError):
            self.lock_path.unlink()
        self._held = False

    def __enter__(self) -> "PidLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    @property
    def is_held_by_self(self) -> bool:
        return self._held
