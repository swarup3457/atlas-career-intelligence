"""Pytest coverage for atlas.orchestration.shutdown.GracefulShutdown
(Phase 0.5 spec section 6)."""

from __future__ import annotations

import signal

import pytest

from atlas.orchestration.shutdown import GracefulShutdown

pytestmark = pytest.mark.unit


def test_trigger_runs_callbacks_in_lifo_order():
    order: list[str] = []
    shutdown = GracefulShutdown(signals=[])
    shutdown.register(lambda: order.append("first-registered"))
    shutdown.register(lambda: order.append("second-registered"))

    shutdown.trigger(reason="test")

    assert order == ["second-registered", "first-registered"]
    assert shutdown.requested is True
    assert shutdown.reason == "test"


def test_trigger_is_idempotent():
    calls = {"count": 0}
    shutdown = GracefulShutdown(signals=[])
    shutdown.register(lambda: calls.__setitem__("count", calls["count"] + 1))

    shutdown.trigger(reason="first")
    shutdown.trigger(reason="second")

    assert calls["count"] == 1
    assert shutdown.reason == "first"  # first reason wins


def test_one_broken_callback_does_not_prevent_others():
    ran: list[str] = []

    def broken():
        raise RuntimeError("simulated cleanup failure")

    shutdown = GracefulShutdown(signals=[])
    shutdown.register(lambda: ran.append("a"))
    shutdown.register(broken)
    shutdown.register(lambda: ran.append("c"))

    shutdown.trigger(reason="test")

    assert ran == ["c", "a"]


def test_context_manager_registers_and_restores_signal_handlers():
    previous_sigint_handler = signal.getsignal(signal.SIGINT)

    with GracefulShutdown(signals=[signal.SIGINT]) as shutdown:
        assert signal.getsignal(signal.SIGINT) is not previous_sigint_handler
        assert shutdown.requested is False

    assert signal.getsignal(signal.SIGINT) is previous_sigint_handler


def test_context_manager_triggers_cleanup_on_normal_exit():
    ran = {"value": False}
    with GracefulShutdown(signals=[]) as shutdown:
        shutdown.register(lambda: ran.__setitem__("value", True))
    assert ran["value"] is True
    assert shutdown.reason == "normal-exit"


def test_simulated_sigint_triggers_cleanup_and_sets_requested_flag():
    """Simulates Ctrl+C by directly invoking the registered signal
    handler (instead of relying on OS signal delivery, which is not
    reliable to test deterministically), proving the wiring is correct."""
    cleaned_up = {"value": False}
    with GracefulShutdown(signals=[signal.SIGINT]) as shutdown:
        shutdown.register(lambda: cleaned_up.__setitem__("value", True))
        shutdown._handle_signal(signal.SIGINT, None)
        assert shutdown.requested is True
        assert cleaned_up["value"] is True
