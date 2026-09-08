"""Phase 1E/F Stage 2 — official Copilot SDK reasoning controller.

All tests run OFFLINE with an injected fake transport; no SDK, network, or
model call ever happens. They assert the controller is optional, least-
privilege, typed, consent-gated, bounded, and never a completion authority.
"""

from __future__ import annotations

import json

import pytest

from atlas.controllers.base import ControllerRequest, NullController, get_controller
from atlas.controllers.copilot import (
    BUILTIN_AGENTS,
    CopilotAgentSpec,
    CopilotSdkController,
    CopilotSdkUnavailable,
    LeastPrivilegePermissionHandler,
    PermissionDecision,
    PermissionDeniedError,
    PrivateDataConsentError,
    RawSdkResult,
    SessionBudgetExceeded,
    build_copilot_controller,
)
from atlas.controllers.operations import CandidateMatchRequest, TypedControllerOps


class FakeTransport:
    """Records every call and returns canned typed output. Implements the
    :class:`CopilotSdkTransport` protocol structurally."""

    def __init__(self, *, models=("claude-opus-4.8", "gpt-5.6"), content="{}",
                 credits=1.0, input_tokens=10, output_tokens=5):
        self._models = list(models)
        self._content = content
        self._credits = credits
        self._in = input_tokens
        self._out = output_tokens
        self.calls: list[dict] = []

    def available_models(self):
        return list(self._models)

    def run(self, *, agent, prompt, context, model, timeout_s, session_id, resume):
        self.calls.append(
            {"agent": agent.name, "prompt": prompt, "context": dict(context),
             "model": model, "timeout_s": timeout_s, "session_id": session_id, "resume": resume}
        )
        return RawSdkResult(
            content=self._content, model=model, session_id=session_id,
            input_tokens=self._in, output_tokens=self._out, credits=self._credits,
        )


class FlakyTimeoutTransport(FakeTransport):
    """Raises TimeoutError for the first ``fail_n`` calls, then succeeds."""

    def __init__(self, fail_n=1, **kw):
        super().__init__(**kw)
        self._fail_n = fail_n
        self._n = 0

    def run(self, **kw):
        self._n += 1
        if self._n <= self._fail_n:
            raise TimeoutError(f"simulated timeout {self._n}")
        return super().run(**kw)


class RaisingTransport(FakeTransport):
    def run(self, **kw):
        raise ValueError("transport blew up")


# --------------------------------------------------------------------------- #
# Disabled-by-default / zero-call guarantees
# --------------------------------------------------------------------------- #
def test_default_controller_is_null_and_makes_no_calls():
    assert isinstance(get_controller("none"), NullController)
    ops = TypedControllerOps()  # defaults to NullController
    res = ops.match_candidate(
        CandidateMatchRequest(job_requirements=("java",), supported_evidence=("java",))
    )
    assert res.metadata.validation_outcome == "NULL_CONTROLLER_DEFAULT"
    # deterministic default still produced a useful, evidence-bounded answer
    assert res.supported == ("java",)


def test_constructing_controller_does_not_call_transport():
    t = FakeTransport()
    CopilotSdkController(transport=t)
    assert t.calls == []  # construction never reasons


# --------------------------------------------------------------------------- #
# Least privilege
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("tool", ["shell", "bash", "powershell", "edit", "write_file",
                                   "git", "network", "http", "fetch", "browser"])
def test_permission_denies_side_effect_tools(tool):
    handler = LeastPrivilegePermissionHandler()
    agent = BUILTIN_AGENTS["triage-ranker"]
    assert handler.check(tool, agent) == PermissionDecision.DENY
    with pytest.raises(PermissionDeniedError):
        handler.assert_allowed(tool, agent)


def test_permission_allows_only_granted_readonly_tools():
    handler = LeastPrivilegePermissionHandler()
    triage = BUILTIN_AGENTS["triage-ranker"]
    assert handler.check("read_job_snapshot", triage) == PermissionDecision.ALLOW
    # a read-only tool NOT granted to this agent is still denied (default-deny)
    assert handler.check("read_lane_policy", triage) == PermissionDecision.DENY


def test_agent_cannot_be_constructed_with_forbidden_tool():
    with pytest.raises(PermissionDeniedError):
        CopilotAgentSpec(name="rogue", prompt="x", allowed_tools=("shell",))
    with pytest.raises(PermissionDeniedError):
        CopilotAgentSpec(name="rogue2", prompt="x", allowed_tools=("read_secret_files",))


# --------------------------------------------------------------------------- #
# Typed output + quarantine + fallback
# --------------------------------------------------------------------------- #
def test_valid_typed_output_not_quarantined():
    t = FakeTransport(content=json.dumps({"scores": [{"job": "a", "score": 80}]}))
    ctrl = CopilotSdkController(transport=t)
    res = ctrl.run_agent("triage-ranker", "rank these", {"jobs": ["a"]})
    assert not res.quarantined
    assert json.loads(res.content)["scores"][0]["score"] == 80
    assert res.model == "claude-opus-4.8"


def test_invalid_json_falls_back_deterministically_via_ops():
    t = FakeTransport(content="not-json-at-all")
    ctrl = CopilotSdkController(transport=t)
    ops = TypedControllerOps(ctrl, model="claude-opus-4.8")
    res = ops.match_candidate(
        CandidateMatchRequest(job_requirements=("java", "aws"), supported_evidence=("java",))
    )
    # invalid controller output -> deterministic fallback, audit finding recorded
    assert res.metadata.validation_outcome == "INVALID_FELL_BACK"
    assert res.supported == ("java",)
    assert res.missing == ("aws",)
    assert ops.audit_findings and ops.audit_findings[0]["outcome"] == "INVALID_FELL_BACK"


def test_empty_content_is_quarantined():
    t = FakeTransport(content="")
    ctrl = CopilotSdkController(transport=t)
    res = ctrl.run_agent("deep-fit-reviewer", "review", {})
    assert res.quarantined
    assert res.content == ""


def test_transport_error_quarantines_not_raises():
    ctrl = CopilotSdkController(transport=RaisingTransport())
    res = ctrl.run_agent("triage-ranker", "rank", {})
    assert res.quarantined
    assert "transport error" in res.quarantine_reason


# --------------------------------------------------------------------------- #
# Timeout / retry budget
# --------------------------------------------------------------------------- #
def test_timeout_retried_within_budget():
    # triage-ranker has retry_budget=1 -> 2 attempts; first times out, second ok
    t = FlakyTimeoutTransport(fail_n=1, content=json.dumps({"ok": True}))
    ctrl = CopilotSdkController(transport=t)
    res = ctrl.run_agent("triage-ranker", "rank", {})
    assert not res.quarantined
    assert json.loads(res.content)["ok"] is True


def test_timeout_exhausts_budget_then_quarantines():
    t = FlakyTimeoutTransport(fail_n=5, content=json.dumps({"ok": True}))
    ctrl = CopilotSdkController(transport=t)
    res = ctrl.run_agent("triage-ranker", "rank", {})
    assert res.quarantined
    assert "timeout" in res.quarantine_reason


# --------------------------------------------------------------------------- #
# Usage metering + budget ceiling
# --------------------------------------------------------------------------- #
def test_usage_metrics_recorded():
    t = FakeTransport(content=json.dumps({"ok": 1}), credits=2.0, input_tokens=100, output_tokens=40)
    ctrl = CopilotSdkController(transport=t, max_session_credits=50)
    ctrl.run_agent("triage-ranker", "rank", {})
    u = ctrl.usage()
    assert u["calls"] == 1
    assert u["input_tokens"] == 100 and u["output_tokens"] == 40
    assert u["credits"] == 2.0
    assert u["events"][0]["outcome"] == "OK"


def test_budget_exceeded_fails_closed():
    t = FakeTransport(content=json.dumps({"ok": 1}), credits=100.0)
    ctrl = CopilotSdkController(transport=t, max_session_credits=5)
    with pytest.raises(SessionBudgetExceeded):
        ctrl.run_agent("triage-ranker", "rank", {})


def test_no_pii_in_usage_events():
    t = FakeTransport(content=json.dumps({"ok": 1}))
    ctrl = CopilotSdkController(transport=t)
    ctrl.run_agent("triage-ranker", "rank this candidate SECRET-NAME", {"resume": "SECRET-RESUME-TEXT"})
    blob = json.dumps(ctrl.usage())
    assert "SECRET-NAME" not in blob
    assert "SECRET-RESUME-TEXT" not in blob
    for ev in ctrl.usage()["events"]:
        assert "prompt" not in ev and "context" not in ev and "candidate" not in ev


# --------------------------------------------------------------------------- #
# Private-data consent gate
# --------------------------------------------------------------------------- #
def test_private_run_without_consent_raises():
    t = FakeTransport(content=json.dumps({"ok": 1}))
    ctrl = CopilotSdkController(transport=t, allow_private_candidate=False, account_type="unspecified")
    with pytest.raises(PrivateDataConsentError):
        ctrl.run_agent("triage-ranker", "rank", {}, private=True)
    # transport was never called with the private payload
    assert t.calls == []


def test_private_by_nature_agent_requires_consent():
    t = FakeTransport(content=json.dumps({"summary": "x", "do_not_claim": []}))
    ctrl = CopilotSdkController(transport=t, allow_private_candidate=False, account_type="unspecified")
    # application-drafter handles_private_candidate=True
    with pytest.raises(PrivateDataConsentError):
        ctrl.run_agent("application-drafter", "draft", {})


def test_private_run_allowed_with_full_consent():
    t = FakeTransport(content=json.dumps({"summary": "x", "do_not_claim": []}))
    ctrl = CopilotSdkController(transport=t, allow_private_candidate=True, account_type="personal")
    assert ctrl.private_data_allowed() is True
    res = ctrl.run_agent("application-drafter", "draft", {"points": ["java"]})
    assert not res.quarantined


def test_consent_requires_both_flag_and_account_ack():
    t = FakeTransport()
    # flag set but account still unspecified -> not allowed
    c1 = CopilotSdkController(transport=t, allow_private_candidate=True, account_type="unspecified")
    assert c1.private_data_allowed() is False
    # account acknowledged but flag off -> not allowed
    c2 = CopilotSdkController(transport=t, allow_private_candidate=False, account_type="organization")
    assert c2.private_data_allowed() is False


# --------------------------------------------------------------------------- #
# Model selection + SDK-unavailable default
# --------------------------------------------------------------------------- #
def test_model_selection_prefers_configured_then_available():
    t = FakeTransport(models=("gpt-5.6", "grok-4.5"))  # no opus
    ctrl = CopilotSdkController(transport=t, model="claude-opus-4.8")
    assert ctrl.select_model() == "gpt-5.6"  # first available fallback
    t2 = FakeTransport(models=("claude-opus-4.8", "gpt-5.6"))
    ctrl2 = CopilotSdkController(transport=t2, model="claude-opus-4.8")
    assert ctrl2.select_model() == "claude-opus-4.8"


def test_official_transport_unavailable_quarantines():
    # No transport provided -> lazily-bound official transport that is not wired.
    ctrl = CopilotSdkController()
    res = ctrl.run_agent("triage-ranker", "rank", {})
    assert res.quarantined
    assert res.usage["events"][-1]["outcome"] == "SDK_UNAVAILABLE"
    assert res.quarantine_reason


def test_build_controller_from_settings():
    from atlas.config import load_settings

    s = load_settings()
    ctrl = build_copilot_controller(s, transport=FakeTransport())
    assert ctrl.configured_model == s.controller_model
    assert ctrl.private_data_allowed() is False  # default settings withhold consent
