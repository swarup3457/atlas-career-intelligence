"""Atlas graceful shutdown handling.

Wires Ctrl+C (SIGINT) and SIGTERM (where the platform supports delivering
it) to a single, idempotent shutdown sequence that:

    1. marks a `shutdown_requested` flag so any running loop can check it
       and stop pulling new work (never marking in-flight/unfinished
       tasks as complete);
    2. runs registered cleanup callbacks in LIFO order (checkpoint save,
       browser close, lock release, log flush, ...), tolerating
       individual callback failures so one broken cleanup step never
       prevents the others from running;
    3. restores the previous signal handlers afterward.

Windows note: SIGTERM can be *registered* via Python's `signal` module,
but Windows does not have a POSIX-style SIGTERM delivery mechanism - a
handler registered for it will only fire if something in-process raises
it (e.g. `os.kill(os.getpid(), signal.SIGTERM)`) or via `signal.raise_signal`.
Ctrl+C (SIGINT) and Ctrl+Break (SIGBREAK) are reliably delivered on
Windows and are always wired.
"""

from __future__ import annotations

import logging
import signal
import sys
from typing import Callable, Optional

logger = logging.getLogger("atlas.orchestration.shutdown")

CleanupCallback = Callable[[], None]


class GracefulShutdown:
    """Registers signal handlers and runs cleanup callbacks exactly once.

    Usage:
        shutdown = GracefulShutdown()
        shutdown.register(lambda: checkpointer.flush())
        shutdown.register(lambda: browser_manager.close())
        with shutdown:
            while not shutdown.requested and remaining:
                ... process one item, checkpoint after each ...
        # cleanup callbacks have already run by the time `with` exits,
        # whether it exited normally or due to a signal.
    """

    def __init__(self, signals: Optional[list[int]] = None):
        self._callbacks: list[CleanupCallback] = []
        self._triggered = False
        self.requested = False
        self.reason: Optional[str] = None
        self._previous_handlers: dict[int, object] = {}

        if signals is None:
            signals = [signal.SIGINT]
            if hasattr(signal, "SIGTERM"):
                signals.append(signal.SIGTERM)
            if sys.platform == "win32" and hasattr(signal, "SIGBREAK"):
                signals.append(signal.SIGBREAK)  # Ctrl+Break on Windows
        self._signals = signals

    def register(self, callback: CleanupCallback) -> None:
        """Register a cleanup callback. Callbacks run in LIFO order (most
        recently registered first), mirroring nested context-manager
        semantics, so e.g. "close browser" (registered last) runs before
        "release profile lock" (registered first) if that ordering
        matters for a particular caller."""
        self._callbacks.append(callback)

    def _handle_signal(self, signum, frame) -> None:  # noqa: ARG002
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = str(signum)
        logger.warning("Received %s - beginning graceful Atlas shutdown.", name)
        self.requested = True
        self.reason = name
        self.trigger(reason=name)

    def trigger(self, reason: str = "manual") -> None:
        """Idempotently run all registered cleanup callbacks (LIFO)."""
        if self._triggered:
            return
        self._triggered = True
        self.requested = True
        if self.reason is None:
            self.reason = reason
        for callback in reversed(self._callbacks):
            try:
                callback()
            except Exception:  # noqa: BLE001
                logger.exception("Shutdown cleanup callback raised; continuing with remaining callbacks.")

    def __enter__(self) -> "GracefulShutdown":
        for sig in self._signals:
            try:
                self._previous_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, self._handle_signal)
            except (ValueError, OSError):
                # e.g. not running in the main thread, or signal unsupported
                # on this platform - degrade gracefully, no crash.
                logger.debug("Could not register handler for signal %s", sig)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.trigger(reason="normal-exit" if exc_type is None else f"exception:{exc_type.__name__}")
        for sig, previous in self._previous_handlers.items():
            try:
                signal.signal(sig, previous)
            except (ValueError, OSError):
                pass
        self._previous_handlers.clear()
