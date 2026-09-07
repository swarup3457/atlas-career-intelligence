"""Atlas browser intervention — visible-escalation mechanism.

When a task needs a human (LOGIN_REQUIRED, SESSION_EXPIRED, MFA_REQUIRED,
CAPTCHA_PRESENT, MANUAL_AUTH_REQUIRED, or another legitimate
human-authentication requirement), Atlas must:

    1. checkpoint the current task (caller's responsibility - this module
       only provides the browser-side escalation primitive)
    2. mark it WAITING_FOR_HUMAN
    3. launch/relaunch that task using channel="chrome", headless=False,
       the dedicated Atlas profile
    4. visibly notify the user what site requires intervention
    5. allow the user to manually authenticate/complete the challenge
    6. never read/store credentials
    7. after confirmation, re-check session state
    8. close the visible browser if appropriate
    9. return to background execution
    10. resume the checkpointed workflow

This module implements steps 3-9 as a reusable function. Steps 1/2/10
(checkpointing and workflow resumption) belong to the orchestration layer
(atlas/orchestration/*) which calls into this module when it detects a
human-intervention terminal status.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from atlas.browser.manager import BrowserManager

logger = logging.getLogger("atlas.browser.intervention")


@dataclass
class InterventionResult:
    resolved: bool
    final_url: Optional[str]
    title: Optional[str]
    notes: str


def escalate_for_human(
    profile_dir: Path,
    channel: str,
    url: str,
    reason: str,
    wait_for_user: Callable[[], None],
    recheck_url: Optional[str] = None,
    notify: Callable[[str], None] = print,
) -> InterventionResult:
    """Perform one visible-escalation cycle.

    Args:
        profile_dir: the dedicated Atlas browser profile directory.
        channel: browser channel (e.g. "chrome").
        url: the URL that needs human attention.
        reason: human-readable reason (e.g. "LOGIN_REQUIRED", "MFA_REQUIRED").
        wait_for_user: callable that blocks until the human indicates they
            are done (e.g. an ``input()`` wrapper). Kept injectable so
            tests can simulate confirmation without a real human.
        recheck_url: URL to re-check session state against after the human
            confirms (defaults to `url`).
        notify: callable used to visibly notify the user (defaults to
            print; callers may pass a logger-backed or UI-backed notifier).

    This function NEVER reads, logs, or stores credentials. It only
    launches a visible browser, waits for a human-provided confirmation
    signal, and re-checks page-level session state (title/URL/status).
    """
    notify(
        f"\n{'=' * 70}\n"
        f"ATLAS NEEDS YOUR HELP: {reason}\n"
        f"Site: {url}\n"
        "A visible Chrome window is opening using your dedicated Atlas\n"
        "profile. Please complete the required sign-in/verification\n"
        "manually. Atlas will never read or store your credentials.\n"
        f"{'=' * 70}\n"
    )

    manager = BrowserManager(profile_dir, channel=channel)
    try:
        page = manager.launch(headless=False)
        manager.navigate(page, url)

        wait_for_user()

        check_url = recheck_url or url
        state = manager.check_session_state(page, check_url)
        limitation = manager.detect_access_limitation(page)

        resolved = limitation is None
        notes = (
            "Session appears resolved after manual intervention."
            if resolved
            else f"Access-limitation signal still present after intervention: {limitation}"
        )
        notify(notes)

        return InterventionResult(
            resolved=resolved,
            final_url=state.get("final_url"),
            title=state.get("title"),
            notes=notes,
        )
    finally:
        manager.close()


@dataclass
class InterventionRequest:
    """One queued request for human intervention. Other tasks that also
    need intervention while this one is pending remain checkpointed
    (their status stays WAITING_FOR_HUMAN/LOGIN_REQUIRED in queue state)
    rather than each popping their own visible browser window."""

    request_id: str
    reason: str
    url: str
    recheck_url: Optional[str] = None


class InterventionQueue:
    """Serializes multiple pending human-intervention requests so that
    AT MOST ONE visible Chrome window is ever open for intervention at a
    time - never one-per-blocked-task.

    Usage:
        queue = InterventionQueue(profile_dir, channel="chrome", wait_for_user=my_wait_fn)
        queue.enqueue(InterventionRequest("task-1", "LOGIN_REQUIRED", "https://example.com"))
        queue.enqueue(InterventionRequest("task-2", "MFA_REQUIRED", "https://example.org"))
        results = queue.process_all()  # processes strictly one at a time
    """

    def __init__(
        self,
        profile_dir: Path,
        channel: str,
        wait_for_user: Callable[[InterventionRequest], None],
        notify: Callable[[str], None] = print,
        escalate_fn: Optional[Callable[..., "InterventionResult"]] = None,
    ):
        self.profile_dir = profile_dir
        self.channel = channel
        self.wait_for_user = wait_for_user
        self.notify = notify
        # Optional per-instance override of the escalation primitive. When
        # None (the default, used by every real caller), process_next()
        # resolves `escalate_for_human` from this module's global
        # namespace at call time (unchanged behavior - existing tests
        # monkeypatch that module attribute directly). A caller MAY pass
        # its own callable here instead - e.g. a demo/test runtime that
        # must simulate human intervention WITHOUT ever launching a real
        # visible browser (see atlas/runtime/engine.py demo mode).
        self._escalate_fn = escalate_fn
        self._pending: list[InterventionRequest] = []
        self._results: dict[str, InterventionResult] = {}
        self._processing_lock = threading.Lock()

    def enqueue(self, request: InterventionRequest) -> None:
        if any(r.request_id == request.request_id for r in self._pending):
            return  # already queued - do not duplicate
        self._pending.append(request)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def results(self) -> dict[str, InterventionResult]:
        return dict(self._results)

    def process_next(self) -> Optional[tuple[InterventionRequest, InterventionResult]]:
        """Process exactly one pending request (if any), opening exactly
        one visible browser session for it, and return (request, result).
        Returns None if the queue is empty. The `_processing_lock` ensures
        two threads can never both open a visible browser concurrently
        even if both call this method at once."""
        with self._processing_lock:
            if not self._pending:
                return None
            request = self._pending.pop(0)
            escalate = self._escalate_fn if self._escalate_fn is not None else escalate_for_human
            result = escalate(
                profile_dir=self.profile_dir,
                channel=self.channel,
                url=request.url,
                reason=request.reason,
                wait_for_user=lambda: self.wait_for_user(request),
                recheck_url=request.recheck_url,
                notify=self.notify,
            )
            self._results[request.request_id] = result
            return request, result

    def process_all(self) -> dict[str, InterventionResult]:
        """Drain the queue, processing one request (one visible browser
        session) at a time until empty."""
        while self._pending:
            self.process_next()
        return self.results()

