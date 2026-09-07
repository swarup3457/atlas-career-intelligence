"""Atlas internal structured event model (LOCAL runtime architecture).

This is NOT the future GitHub persistence event schema - it is a small,
in-process (and optionally file-appended) event stream describing what
happened during a run, useful today for logging/debugging, and later for
metrics, GitHub export, and Excel run summaries without those consumers
needing to re-derive events from raw logs.

No business-specific event types are defined here (e.g. no
"JOB_FOUND_AT_COMPANY_X") - only generic run/task lifecycle events.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


class EventType:
    """Generic Atlas runtime event type constants."""

    RUN_STARTED = "RUN_STARTED"
    RUN_RESUMED = "RUN_RESUMED"
    TASK_STARTED = "TASK_STARTED"
    TASK_RETRY = "TASK_RETRY"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_FAILED = "TASK_FAILED"
    ACCESS_LIMITED = "ACCESS_LIMITED"
    HUMAN_INTERVENTION_REQUIRED = "HUMAN_INTERVENTION_REQUIRED"
    HUMAN_INTERVENTION_RESOLVED = "HUMAN_INTERVENTION_RESOLVED"
    CHECKPOINT_SAVED = "CHECKPOINT_SAVED"
    RUN_PARTIAL = "RUN_PARTIAL"
    RUN_COMPLETED = "RUN_COMPLETED"


ALL_EVENT_TYPES = frozenset(
    v for k, v in vars(EventType).items() if not k.startswith("_") and isinstance(v, str)
)


@dataclass(frozen=True)
class Event:
    event_type: str
    run_id: str
    timestamp: str
    task_id: Optional[str] = None
    company: Optional[str] = None
    attempt: Optional[int] = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


Subscriber = Callable[[Event], None]


class EventBus:
    """Minimal in-process publish/subscribe event bus.

    Thread-safe for the simple case of one publisher thread and a
    handful of subscribers (logging, metrics, optional file sink) - not
    designed for high-throughput multi-producer workloads, which Atlas
    does not have.
    """

    def __init__(self, run_id: str, sink_path: Optional[Path] = None):
        self.run_id = run_id
        self.sink_path = Path(sink_path) if sink_path else None
        self._subscribers: list[Subscriber] = []
        self._events: list[Event] = []
        self._lock = threading.Lock()
        if self.sink_path is not None:
            self.sink_path.parent.mkdir(parents=True, exist_ok=True)

    def subscribe(self, subscriber: Subscriber) -> None:
        with self._lock:
            self._subscribers.append(subscriber)

    def publish(
        self,
        event_type: str,
        task_id: Optional[str] = None,
        company: Optional[str] = None,
        attempt: Optional[int] = None,
        detail: Optional[dict[str, Any]] = None,
    ) -> Event:
        if event_type not in ALL_EVENT_TYPES:
            raise ValueError(f"Unknown Atlas event type: {event_type!r}")
        event = Event(
            event_type=event_type,
            run_id=self.run_id,
            timestamp=_utcnow(),
            task_id=task_id,
            company=company,
            attempt=attempt,
            detail=detail or {},
        )
        with self._lock:
            self._events.append(event)
            subscribers = list(self._subscribers)
        if self.sink_path is not None:
            with self.sink_path.open("a", encoding="utf-8") as f:
                f.write(event.to_json() + "\n")
        for subscriber in subscribers:
            subscriber(event)
        return event

    def events(self) -> list[Event]:
        with self._lock:
            return list(self._events)
