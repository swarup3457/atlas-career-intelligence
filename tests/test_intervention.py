"""Pytest coverage for atlas.browser.intervention.InterventionQueue
(Phase 0.5 spec section 17). These tests never open a real browser -
`escalate_for_human` is monkeypatched so the queue's serialization logic
is verified deterministically and offline."""

from __future__ import annotations

import threading
import time

import pytest

from atlas.browser import intervention as intervention_module
from atlas.browser.intervention import InterventionQueue, InterventionRequest, InterventionResult

pytestmark = pytest.mark.unit


def test_enqueue_deduplicates_by_request_id(tmp_path):
    queue = InterventionQueue(tmp_path / "profile", channel="chrome", wait_for_user=lambda req: None)
    queue.enqueue(InterventionRequest("task-1", "LOGIN_REQUIRED", "https://example.com"))
    queue.enqueue(InterventionRequest("task-1", "LOGIN_REQUIRED", "https://example.com"))
    assert queue.pending_count == 1


def test_process_next_returns_none_when_empty(tmp_path):
    queue = InterventionQueue(tmp_path / "profile", channel="chrome", wait_for_user=lambda req: None)
    assert queue.process_next() is None


def test_process_all_handles_each_request_exactly_once(tmp_path, monkeypatch):
    calls: list[str] = []

    def fake_escalate(*, profile_dir, channel, url, reason, wait_for_user, recheck_url=None, notify=print):
        calls.append(url)
        wait_for_user()
        return InterventionResult(resolved=True, final_url=url, title="stub", notes="stub-resolved")

    monkeypatch.setattr(intervention_module, "escalate_for_human", fake_escalate)

    queue = InterventionQueue(tmp_path / "profile", channel="chrome", wait_for_user=lambda req: None)
    queue.enqueue(InterventionRequest("task-1", "LOGIN_REQUIRED", "https://example.com"))
    queue.enqueue(InterventionRequest("task-2", "MFA_REQUIRED", "https://example.org"))

    results = queue.process_all()

    assert calls == ["https://example.com", "https://example.org"]
    assert set(results.keys()) == {"task-1", "task-2"}
    assert all(r.resolved for r in results.values())
    assert queue.pending_count == 0


def test_only_one_visible_session_active_at_a_time(tmp_path, monkeypatch):
    """Two threads racing process_next() must never overlap inside the
    escalation call - the internal lock must fully serialize them."""
    concurrent_count = {"value": 0, "max": 0}
    lock = threading.Lock()

    def fake_escalate(*, profile_dir, channel, url, reason, wait_for_user, recheck_url=None, notify=print):
        with lock:
            concurrent_count["value"] += 1
            concurrent_count["max"] = max(concurrent_count["max"], concurrent_count["value"])
        time.sleep(0.05)
        with lock:
            concurrent_count["value"] -= 1
        wait_for_user()
        return InterventionResult(resolved=True, final_url=url, title="stub", notes="stub")

    monkeypatch.setattr(intervention_module, "escalate_for_human", fake_escalate)

    queue = InterventionQueue(tmp_path / "profile", channel="chrome", wait_for_user=lambda req: None)
    for i in range(4):
        queue.enqueue(InterventionRequest(f"task-{i}", "LOGIN_REQUIRED", f"https://example.com/{i}"))

    threads = [threading.Thread(target=queue.process_next) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert concurrent_count["max"] == 1
    assert queue.pending_count == 0
