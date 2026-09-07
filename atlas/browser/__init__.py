"""Atlas browser package: BrowserManager (Chrome lifecycle owner) and the
visible-escalation intervention mechanism."""

from atlas.browser.manager import BrowserManager, ProfileLockedError, NavigationRecord
from atlas.browser.intervention import (
    InterventionResult,
    InterventionRequest,
    InterventionQueue,
    escalate_for_human,
)

__all__ = [
    "BrowserManager",
    "ProfileLockedError",
    "NavigationRecord",
    "InterventionResult",
    "InterventionRequest",
    "InterventionQueue",
    "escalate_for_human",
]
