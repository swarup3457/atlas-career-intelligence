"""Pytest coverage for atlas.runtime.states (Phase 0.75 spec section 3/21)."""

from __future__ import annotations

import pytest

from atlas.runtime.states import (
    ALLOWED_TRANSITIONS,
    IllegalRunStateTransition,
    RunState,
    is_terminal,
    transition,
)

pytestmark = pytest.mark.unit


def test_initializing_to_running_allowed():
    assert transition(RunState.INITIALIZING, RunState.RUNNING) == RunState.RUNNING


def test_illegal_transition_raises():
    with pytest.raises(IllegalRunStateTransition):
        transition(RunState.COMPLETE, RunState.RUNNING)


def test_complete_and_failed_are_terminal_with_no_outgoing_transitions():
    assert ALLOWED_TRANSITIONS[RunState.COMPLETE] == frozenset()
    assert ALLOWED_TRANSITIONS[RunState.FAILED] == frozenset()
    assert is_terminal(RunState.COMPLETE)
    assert is_terminal(RunState.FAILED)
    assert not is_terminal(RunState.RUNNING)


def test_full_happy_path_lifecycle():
    state = RunState.INITIALIZING
    state = transition(state, RunState.RUNNING)
    state = transition(state, RunState.FINALIZING)
    state = transition(state, RunState.COMPLETE)
    assert state == RunState.COMPLETE


def test_partial_then_resume_then_complete():
    state = RunState.INITIALIZING
    state = transition(state, RunState.RUNNING)
    state = transition(state, RunState.PARTIAL)
    state = transition(state, RunState.RESUMING)
    state = transition(state, RunState.RUNNING)
    state = transition(state, RunState.FINALIZING)
    state = transition(state, RunState.WAITING_FOR_HUMAN)
    assert state == RunState.WAITING_FOR_HUMAN


def test_every_state_has_a_table_entry():
    for state in RunState:
        assert state in ALLOWED_TRANSITIONS
