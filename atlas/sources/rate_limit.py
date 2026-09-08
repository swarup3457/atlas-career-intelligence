"""Atlas per-source rate limiting (Phase 1A).

Purpose: reliability, politeness, and resource control — NEVER anti-detection.
There is deliberately no random "human-like" jitter and no attempt to mask
automation. Behavior is fully deterministic and testable via injected clock
and sleeper callables.

Capabilities:
    * minimum interval between requests to the same source instance
    * per-instance concurrency cap
    * honoring an explicit ``Retry-After`` from a 429 response
    * an adaptive backoff hook that grows the not-before delay on repeated
      rate-limit signals and resets on success (bounded, never unbounded)
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional


class RateLimitConfigError(ValueError):
    """Raised when a rate policy is invalid (e.g. negative interval, zero
    concurrency). Dangerous config is never silently accepted."""


@dataclass(frozen=True)
class RatePolicy:
    """Declarative per-source rate policy. All times are in seconds."""

    min_interval_seconds: float = 0.0
    max_concurrency: int = 1
    backoff_initial_seconds: float = 1.0
    backoff_max_seconds: float = 60.0
    backoff_multiplier: float = 2.0

    def validate(self) -> None:
        problems: list[str] = []
        if self.min_interval_seconds < 0:
            problems.append(f"min_interval_seconds must be >= 0 (got {self.min_interval_seconds}).")
        if self.max_concurrency < 1:
            problems.append(f"max_concurrency must be >= 1 (got {self.max_concurrency}).")
        if self.backoff_initial_seconds <= 0:
            problems.append(f"backoff_initial_seconds must be > 0 (got {self.backoff_initial_seconds}).")
        if self.backoff_max_seconds < self.backoff_initial_seconds:
            problems.append("backoff_max_seconds must be >= backoff_initial_seconds.")
        if self.backoff_multiplier < 1.0:
            problems.append(f"backoff_multiplier must be >= 1.0 (got {self.backoff_multiplier}).")
        if problems:
            raise RateLimitConfigError("; ".join(problems))


class RateLimiter:
    """Deterministic per-instance-key rate limiter.

    ``clock`` returns a monotonically increasing float (seconds); ``sleeper``
    consumes a float delay. Both are injectable so tests use a virtual clock
    and never actually sleep.
    """

    def __init__(
        self,
        policy: RatePolicy,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        policy.validate()
        self.policy = policy
        self._clock = clock
        self._sleeper = sleeper
        self._last_start: dict[str, float] = {}
        self._not_before: dict[str, float] = {}
        self._active: dict[str, int] = {}
        self._backoff: dict[str, float] = {}
        # A Condition (lock + wait/notify) so a blocking slot acquisition can
        # wait for a slot to free instead of busy-spinning. It is also a valid
        # plain lock for every other critical section below.
        self._lock = threading.Condition()

    # -- pacing -------------------------------------------------------------
    def next_available(self, key: str) -> float:
        """Absolute clock time at which the next request to ``key`` may
        start (does not sleep)."""
        earliest = self._last_start.get(key, float("-inf")) + self.policy.min_interval_seconds
        return max(earliest, self._not_before.get(key, float("-inf")))

    def acquire(self, key: str) -> float:
        """Block (via the injected sleeper) until a request to ``key`` may
        start; returns the number of seconds waited."""
        with self._lock:
            now = self._clock()
            target = self.next_available(key)
            wait = target - now
            if wait == float("inf"):  # pragma: no cover - defensive
                wait = 0.0
            wait = max(0.0, wait)
        if wait > 0:
            self._sleeper(wait)
        with self._lock:
            self._last_start[key] = self._clock()
        return wait

    # -- Retry-After / adaptive backoff ------------------------------------
    def note_retry_after(self, key: str, seconds: float) -> None:
        """Honor an explicit ``Retry-After`` (seconds) from the source."""
        if seconds < 0:
            raise RateLimitConfigError("Retry-After seconds must be >= 0.")
        with self._lock:
            self._not_before[key] = self._clock() + seconds

    def note_rate_limited(self, key: str) -> float:
        """Signal a 429/soft-limit without an explicit Retry-After. Grows an
        adaptive, bounded backoff and returns the applied delay (seconds)."""
        with self._lock:
            current = self._backoff.get(key, 0.0)
            if current <= 0:
                nxt = self.policy.backoff_initial_seconds
            else:
                nxt = min(current * self.policy.backoff_multiplier, self.policy.backoff_max_seconds)
            self._backoff[key] = nxt
            self._not_before[key] = self._clock() + nxt
            return nxt

    def note_success(self, key: str) -> None:
        """Reset adaptive backoff after a clean request."""
        with self._lock:
            self._backoff.pop(key, None)

    # -- concurrency --------------------------------------------------------
    def at_capacity(self, key: str) -> bool:
        with self._lock:
            return self._active.get(key, 0) >= self.policy.max_concurrency

    def acquire_slot(self, key: str, *, blocking: bool = False, timeout: Optional[float] = None) -> bool:
        """Reserve a concurrency slot for ``key``.

        With ``blocking=False`` (the default, preserving the original
        contract) it returns ``False`` immediately when already at capacity.
        With ``blocking=True`` it waits (via the internal condition, releasing
        the lock while it waits) until a slot frees or ``timeout`` elapses,
        returning ``True`` only when a slot was actually reserved. Callers MUST
        honor the return value: a ``False`` result means NO slot was acquired
        and the caller must neither proceed nor later release a slot it never
        held."""
        deadline = None if (timeout is None or not blocking) else (time.monotonic() + timeout)
        with self._lock:
            while True:
                active = self._active.get(key, 0)
                if active < self.policy.max_concurrency:
                    self._active[key] = active + 1
                    return True
                if not blocking:
                    return False
                wait = None
                if deadline is not None:
                    wait = deadline - time.monotonic()
                    if wait <= 0:
                        return False
                # wait() releases the lock while blocked and re-acquires on wake.
                self._lock.wait(timeout=wait)

    def release_slot(self, key: str) -> None:
        with self._lock:
            active = self._active.get(key, 0)
            if active > 0:
                self._active[key] = active - 1
            # Wake a blocked acquirer, if any, now that a slot may be free.
            self._lock.notify_all()

    def active_count(self, key: str) -> int:
        with self._lock:
            return self._active.get(key, 0)


class ManualClock:
    """A deterministic virtual clock + sleeper for tests. ``sleep`` simply
    advances the clock; no real time passes."""

    def __init__(self, start: float = 0.0):
        self._t = float(start)

    def time(self) -> float:
        return self._t

    def sleep(self, seconds: float) -> None:
        self._t += max(0.0, float(seconds))

    def advance(self, seconds: float) -> None:
        self._t += float(seconds)


__all__ = [
    "RateLimitConfigError",
    "RatePolicy",
    "RateLimiter",
    "ManualClock",
]
