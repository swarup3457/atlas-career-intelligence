"""Injectable clock for deterministic Phase 0.95 timestamps.

Backup identity (``backup_id``) and ``created_at`` are derived from "now".
To keep those reproducible in tests *without* monkeypatching the global
``datetime`` module (which is fragile and leaks across tests), all new
Phase 0.95 code reads the current time through a small :class:`Clock`
protocol. Production uses :class:`SystemClock`; tests pass a
:class:`FixedClock` or :class:`StepClock`.

This is intentionally NOT wired into any pre-existing, already-proven
module — only new Phase 0.95 code depends on it.
"""

from __future__ import annotations

import datetime
from typing import Protocol, runtime_checkable

_UTC = datetime.timezone.utc


@runtime_checkable
class Clock(Protocol):
    """Anything that can report the current UTC instant."""

    def utcnow(self) -> datetime.datetime:  # pragma: no cover - protocol
        ...


class SystemClock:
    """Real wall-clock time, always timezone-aware UTC."""

    def utcnow(self) -> datetime.datetime:
        return datetime.datetime.now(_UTC)


class FixedClock:
    """A clock frozen at a single instant (deterministic tests)."""

    def __init__(self, moment: datetime.datetime):
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=_UTC)
        self._moment = moment.astimezone(_UTC)

    def utcnow(self) -> datetime.datetime:
        return self._moment


class StepClock:
    """A clock that advances by a fixed step on every read.

    Useful for asserting that two operations get strictly increasing,
    still-deterministic timestamps (e.g. proving backup ids sort in
    creation order) without relying on real elapsed time.
    """

    def __init__(
        self,
        start: datetime.datetime,
        step: datetime.timedelta = datetime.timedelta(seconds=1),
    ):
        if start.tzinfo is None:
            start = start.replace(tzinfo=_UTC)
        self._next = start.astimezone(_UTC)
        self._step = step

    def utcnow(self) -> datetime.datetime:
        current = self._next
        self._next = self._next + self._step
        return current


def resolve_clock(clock: Clock | None) -> Clock:
    """Return ``clock`` if given, otherwise a fresh :class:`SystemClock`."""
    return clock if clock is not None else SystemClock()
