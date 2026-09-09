"""Official GitHub Copilot SDK reasoning controller (Phase 1E/F §7).

This replaces the earlier documented placeholder with a REAL, OPTIONAL,
least-privilege, typed reasoning controller. The controller is DISABLED by
default (``controller='none'``); deterministic Atlas runs never construct it
and never call a model. When explicitly enabled it drives short, typed
reasoning sessions through the official Copilot SDK — and NOTHING here is ever
a completion authority: Python/LangGraph decide what is done, always.

Design guarantees (all offline-testable via an injected transport):

* **Optional transport seam.** The controller talks to a
  :class:`CopilotSdkTransport`. The official-SDK transport lazily imports the
  pinned package only on first real use, so importing/constructing this module
  never requires the SDK to be installed. Deterministic mode + tests inject a
  fake transport; no network or model call happens.
* **Least privilege.** A default-DENY permission handler exposes only a small
  allowlist of typed, local, read-only tools per agent. Shell, file-edit, Git,
  and arbitrary network tools are ALWAYS denied — candidate-reasoning agents
  cannot touch the repository, output directory, or the network.
* **Custom typed agents.** Four narrow agents (``triage-ranker``,
  ``deep-fit-reviewer``, ``application-drafter``, ``factual-grounding-reviewer``)
  each carry a narrow prompt, an explicit tool list, a timeout, a retry budget,
  a deterministic fallback, and invalid-output quarantine.
* **Bounded usage.** A per-session :class:`UsageMeter` records model, tokens,
  credits, latency, and session id, and fails closed when the configured credit
  budget is exceeded.
* **Private-data consent gate.** Sending the real candidate profile requires
  BOTH an explicit operator flag AND an account-type acknowledgement. Without
  consent, private payloads raise :class:`PrivateDataConsentError` and callers
  fall back to deterministic matching / a synthetic canary.
* **No PII in metric logs.** The session event log records only structured
  metrics (agent, model, tokens, latency, outcome, session id) — never the
  prompt, context, or candidate payload.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

from atlas.controllers.base import BaseController, ControllerRequest, ControllerResponse


def _json_dumps(obj: Any) -> str:
    import json

    try:
        return json.dumps(obj, default=str, sort_keys=True)[:8000]
    except (TypeError, ValueError):
        return str(obj)[:8000]


def _strip_code_fences(text: str) -> str:
    """Strip a leading/trailing markdown code fence (```json ... ```), which real
    LLM output commonly wraps JSON in, so the deterministic JSON validators can
    parse it. A bare (unfenced) response is returned unchanged, so fake-transport
    tests that already emit bare JSON are unaffected."""
    import re

    s = (text or "").strip()
    m = re.match(r"^```[a-zA-Z0-9_-]*\s*\n?(.*?)\n?```$", s, re.DOTALL)
    if m:
        return m.group(1).strip()
    return s

# The official GitHub Copilot Python SDK distribution (verified pass 4):
#   pip package : github-copilot-sdk   (author: GitHub <opensource@github.com>)
#   import name : copilot              (from copilot import CopilotClient)
#   homepage    : https://github.com/github/copilot-sdk
#   license     : MIT
# It is imported LAZILY (only inside the transport methods) so Atlas never
# hard-depends on it: deterministic runs and the entire offline test suite work
# without ever loading or invoking it.
OFFICIAL_SDK_PACKAGE = "copilot"          # import name of github-copilot-sdk
OFFICIAL_SDK_DISTRIBUTION = "github-copilot-sdk"
OFFICIAL_SDK_PINNED_VERSION = "1.0.13"
OFFICIAL_SDK_LICENSE = "MIT"

# Tools that are NEVER permitted for any reasoning agent, regardless of the
# per-agent allowlist. A candidate-reasoning model gets no side-effect power.
FORBIDDEN_TOOLS: frozenset[str] = frozenset(
    {
        "shell", "bash", "powershell", "cmd", "exec", "run",
        "edit", "write", "write_file", "create_file", "delete_file", "fs_write",
        "git", "commit", "push", "checkout",
        "network", "http", "https", "fetch", "curl", "browser", "playwright",
        "socket", "dns", "email", "send",
    }
)

# The complete universe of typed, local, READ-ONLY tools an agent may be granted.
# Every one is a pure read of already-loaded, in-memory, redacted evidence — no
# I/O. An agent is granted a SUBSET of these; anything outside is denied.
LOCAL_READONLY_TOOLS: frozenset[str] = frozenset(
    {
        "read_job_snapshot",       # already-fetched job posting fields
        "read_job_requirements",   # extracted mandatory/preferred requirements
        "read_candidate_evidence", # redacted candidate strengths (topic/class only)
        "read_lane_policy",        # configured lane/eligibility policy
        "read_match_axes",         # deterministic eligibility/verification/freshness axes
    }
)


class CopilotSdkUnavailable(RuntimeError):
    """The official Copilot SDK could not be imported / is not installed."""


class PrivateDataConsentError(PermissionError):
    """A private (real candidate) payload would be sent to a model without the
    required explicit operator consent + account-type acknowledgement."""


class SessionBudgetExceeded(RuntimeError):
    """A reasoning session exceeded its bounded per-session credit budget."""


class PermissionDeniedError(PermissionError):
    """An agent requested a tool outside its least-privilege allowlist."""


# --------------------------------------------------------------------------- #
# Transport seam
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RawSdkResult:
    """The neutral shape the controller consumes from any transport."""

    content: str
    model: str
    session_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    credits: float = 0.0


@runtime_checkable
class CopilotSdkTransport(Protocol):
    """Minimal transport the controller depends on. The official SDK adapter and
    all test fakes implement this; the controller never imports the SDK
    directly."""

    def available_models(self) -> Sequence[str]:
        """Return the model ids the current account can use (may be empty)."""
        ...

    def run(
        self,
        *,
        agent: "CopilotAgentSpec",
        prompt: str,
        context: Mapping[str, Any],
        model: str,
        timeout_s: float,
        session_id: str,
        resume: bool,
    ) -> RawSdkResult:
        """Execute one bounded reasoning turn and return a :class:`RawSdkResult`.
        May raise ``TimeoutError`` (retryable) or any exception (quarantined)."""


class _OfficialCopilotSdkTransport:
    """Lazily-bound adapter over the official Copilot SDK.

    The SDK is imported only here, on first use, so the rest of Atlas has no
    import-time dependency on it. If it is not installed, a clear
    :class:`CopilotSdkUnavailable` is raised with install guidance rather than a
    bare ``ImportError``.
    """

    def __init__(self, *, base_directory: Optional[str] = None,
                 model_list_timeout_s: float = 30.0) -> None:
        self._sdk: Any = None
        # COPILOT_HOME for the spawned runtime (session state/config). Defaults to
        # the SDK default (~/.copilot). Set to a writable dir in restricted envs.
        self.base_directory = base_directory
        self.model_list_timeout_s = float(model_list_timeout_s)

    def _load(self) -> Any:
        if self._sdk is not None:
            return self._sdk
        try:  # pragma: no cover - exercised only with the real SDK installed
            import importlib

            self._sdk = importlib.import_module(OFFICIAL_SDK_PACKAGE)
        except Exception as exc:  # noqa: BLE001
            raise CopilotSdkUnavailable(
                f"The official Copilot SDK package {OFFICIAL_SDK_PACKAGE!r} is not "
                "installed/importable. Install and pin it before enabling live "
                "Copilot reasoning (controller='copilot'); deterministic runs do "
                "not require it."
            ) from exc
        return self._sdk

    def available_models(self) -> Sequence[str]:  # pragma: no cover - live only
        """Query the account's available model ids via the official SDK.

        Runs a bounded async client purely to list models. Any failure (SDK not
        installed, no runtime, no auth) surfaces as an empty list / exception the
        caller treats as "unknown", never a crash of deterministic Atlas."""
        self._load()
        import asyncio

        async def _list() -> list[str]:
            from copilot import CopilotClient

            async with CopilotClient() as client:
                models = await client.list_models()
                out: list[str] = []
                for m in models:
                    mid = getattr(m, "id", None)
                    if mid:
                        out.append(str(mid))
                return out

        try:
            return asyncio.run(asyncio.wait_for(_list(), timeout=self.model_list_timeout_s))
        except Exception:  # noqa: BLE001 - availability is best-effort
            return []

    def run(  # pragma: no cover - live only
        self,
        *,
        agent: "CopilotAgentSpec",
        prompt: str,
        context: Mapping[str, Any],
        model: str,
        timeout_s: float,
        session_id: str,
        resume: bool,
    ) -> RawSdkResult:
        """Execute ONE bounded, least-privilege reasoning turn via the official
        SDK and return a neutral :class:`RawSdkResult`.

        Least privilege is enforced at the SDK boundary: the session is created
        with ``available_tools=[]`` (the model is granted NO tools) and a
        default-DENY ``on_permission_request`` handler, so a reasoning agent can
        never invoke shell/edit/git/network/browser tools even if it tried. The
        candidate context is passed as text in the prompt only; no repository or
        filesystem access is granted."""
        self._load()
        import asyncio

        payload = f"{prompt}\n\nStructured input (JSON):\n{_json_dumps(context)}"

        async def _run() -> RawSdkResult:
            from copilot import CopilotClient
            from copilot.session_events import AssistantMessageData, SessionIdleData

            collected: list[str] = []
            output_tokens = 0
            model_used = model
            done = asyncio.Event()

            def _on_event(event) -> None:
                data = getattr(event, "data", None)
                if isinstance(data, AssistantMessageData):
                    if data.content:
                        collected.append(str(data.content))
                    nonlocal output_tokens, model_used
                    output_tokens += int(getattr(data, "output_tokens", 0) or 0)
                    if getattr(data, "model", None):
                        model_used = str(data.model)
                elif isinstance(data, SessionIdleData):
                    done.set()

            async def _deny_permission(_request):
                # default-DENY: a reasoning agent gets no tool side effects.
                raise PermissionError("tool use denied for reasoning-only session")

            async with CopilotClient(base_directory=self.base_directory) as client:
                session = await client.create_session(
                    model=model,
                    available_tools=[],            # NO tools -> pure reasoning
                    on_permission_request=_deny_permission,
                    on_event=_on_event,
                    streaming=False,
                )
                async with session:
                    await session.send(payload)
                    await done.wait()
            return RawSdkResult(
                content="".join(collected), model=model_used, session_id=session_id,
                input_tokens=0, output_tokens=output_tokens, credits=0.0,
            )

        try:
            return asyncio.run(asyncio.wait_for(_run(), timeout=max(1.0, timeout_s)))
        except CopilotSdkUnavailable:
            raise
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"copilot session timed out after {timeout_s}s") from exc
        except Exception as exc:  # noqa: BLE001 - surfaced to the controller as a quarantine
            raise CopilotSdkUnavailable(
                f"official Copilot SDK session could not run in this environment: {exc}. "
                "Ensure the runtime is provisioned (python -m copilot download-runtime) and "
                "the account is authenticated; deterministic runs do not require it."
            ) from exc


# --------------------------------------------------------------------------- #
# Agents + permissions
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CopilotAgentSpec:
    """A narrow, typed custom agent. Everything about what it may do is explicit
    and bounded — prompt, tool allowlist, timeout, and retry budget."""

    name: str
    prompt: str
    allowed_tools: tuple[str, ...] = ()
    timeout_s: float = 60.0
    retry_budget: int = 1
    # Whether this agent's normal payload contains real candidate PII (subject
    # to the private-data consent gate). Deterministic fallbacks never do.
    handles_private_candidate: bool = False

    def __post_init__(self) -> None:
        bad = [t for t in self.allowed_tools if t in FORBIDDEN_TOOLS]
        if bad:
            raise PermissionDeniedError(
                f"agent {self.name!r} may not be granted forbidden tools: {bad}"
            )
        outside = [t for t in self.allowed_tools if t not in LOCAL_READONLY_TOOLS]
        if outside:
            raise PermissionDeniedError(
                f"agent {self.name!r} may only use typed local read-only tools; "
                f"not in the allowlist universe: {outside}"
            )


# The four custom agents. Prompts are deliberately terse: they instruct the
# model to reason ONLY over the compact typed evidence provided and to emit a
# single strict JSON object. Grounding/validation is enforced in Python.
BUILTIN_AGENTS: dict[str, CopilotAgentSpec] = {
    "triage-ranker": CopilotAgentSpec(
        name="triage-ranker",
        prompt=(
            "You rank a small batch of job postings for one candidate. Use ONLY "
            "the compact typed evidence provided. For each job return an integer "
            "score 0-100, evidence-backed strengths and gaps, and any hard "
            "location/eligibility veto. Emit one strict JSON object. Never invent "
            "requirements, employers, or candidate experience. You do not decide "
            "completion."
        ),
        allowed_tools=("read_job_snapshot", "read_job_requirements",
                       "read_candidate_evidence", "read_match_axes"),
        timeout_s=45.0,
        retry_budget=1,
    ),
    "deep-fit-reviewer": CopilotAgentSpec(
        name="deep-fit-reviewer",
        prompt=(
            "You perform a deep fit review of ONE job for one candidate using "
            "ONLY the typed evidence provided. Compare mandatory and preferred "
            "requirements against candidate evidence, list factual gaps, and "
            "recommend one of the allowed recommendation states. Emit one strict "
            "JSON object. Never fabricate evidence; never call this an ATS score."
        ),
        allowed_tools=("read_job_snapshot", "read_job_requirements",
                       "read_candidate_evidence", "read_match_axes", "read_lane_policy"),
        timeout_s=60.0,
        retry_budget=1,
    ),
    "application-drafter": CopilotAgentSpec(
        name="application-drafter",
        prompt=(
            "You draft a factual application brief for ONE job using ONLY the "
            "supported candidate points provided. You must NOT assert any point "
            "listed as missing/unsupported. Emit one strict JSON object with the "
            "brief and an explicit do-not-claim list. This is a LOCAL DRAFT; it "
            "is never submitted."
        ),
        allowed_tools=("read_job_snapshot", "read_candidate_evidence"),
        timeout_s=60.0,
        retry_budget=1,
        handles_private_candidate=True,
    ),
    "factual-grounding-reviewer": CopilotAgentSpec(
        name="factual-grounding-reviewer",
        prompt=(
            "You audit drafted application content against a set of evidence IDs. "
            "Flag every date, employer, title, metric, or technology that is not "
            "supported by an evidence ID. Emit one strict JSON object listing "
            "supported and unsupported claims. You never approve unsupported "
            "facts."
        ),
        allowed_tools=("read_candidate_evidence", "read_job_requirements"),
        timeout_s=60.0,
        retry_budget=1,
    ),
}


class PermissionDecision(str):
    ALLOW = "ALLOW"
    DENY = "DENY"


class LeastPrivilegePermissionHandler:
    """Default-DENY tool permission handler. A tool is permitted only when it is
    in the agent's explicit allowlist AND in the local read-only universe AND
    not globally forbidden. Shell/edit/git/network are therefore always denied.
    """

    def check(self, tool: str, agent: CopilotAgentSpec) -> str:
        t = (tool or "").strip().lower()
        if not t:
            return PermissionDecision.DENY
        if t in FORBIDDEN_TOOLS:
            return PermissionDecision.DENY
        if t not in LOCAL_READONLY_TOOLS:
            return PermissionDecision.DENY
        if t not in agent.allowed_tools:
            return PermissionDecision.DENY
        return PermissionDecision.ALLOW

    def allowed(self, tool: str, agent: CopilotAgentSpec) -> bool:
        return self.check(tool, agent) == PermissionDecision.ALLOW

    def assert_allowed(self, tool: str, agent: CopilotAgentSpec) -> None:
        if not self.allowed(tool, agent):
            raise PermissionDeniedError(
                f"tool {tool!r} denied for agent {agent.name!r} (least-privilege default-deny)"
            )


# --------------------------------------------------------------------------- #
# Usage metering
# --------------------------------------------------------------------------- #
@dataclass
class UsageMeter:
    """Bounded per-controller usage accounting. Fails closed when the configured
    credit budget is exceeded. Records only non-PII structured metrics."""

    max_session_credits: float
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    credits: float = 0.0
    latency_ms_total: float = 0.0
    events: list[dict[str, Any]] = field(default_factory=list)

    def would_exceed(self, credits: float) -> bool:
        if self.max_session_credits < 0:
            return False
        return (self.credits + credits) > self.max_session_credits

    def record(
        self,
        *,
        agent: str,
        model: str,
        session_id: str,
        input_tokens: int,
        output_tokens: int,
        credits: float,
        latency_ms: float,
        outcome: str,
    ) -> None:
        self.calls += 1
        self.input_tokens += int(input_tokens)
        self.output_tokens += int(output_tokens)
        self.credits += float(credits)
        self.latency_ms_total += float(latency_ms)
        # NOTE: no prompt/context/candidate content is ever stored here.
        self.events.append(
            {
                "agent": agent,
                "model": model,
                "session_id": session_id,
                "input_tokens": int(input_tokens),
                "output_tokens": int(output_tokens),
                "credits": float(credits),
                "latency_ms": round(float(latency_ms), 2),
                "outcome": outcome,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "credits": round(self.credits, 4),
            "latency_ms_total": round(self.latency_ms_total, 2),
            "max_session_credits": self.max_session_credits,
            "events": list(self.events),
        }


@dataclass(frozen=True)
class CopilotSessionResult:
    """Uniform typed result of one agent run. ``quarantined`` marks a failed /
    invalid session whose content must NOT be trusted; callers deterministically
    fall back. ``content`` is always a string (empty on quarantine)."""

    agent: str
    content: str
    model: str
    session_id: str
    latency_ms: float
    quarantined: bool = False
    quarantine_reason: str = ""
    usage: Mapping[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Controller
# --------------------------------------------------------------------------- #
class CopilotSdkController(BaseController):
    """Optional, least-privilege, typed Copilot reasoning controller.

    Implements the generic :class:`~atlas.controllers.base.Controller` protocol
    (``reason``/``classify``/``extract``/``review``) so it plugs into
    :class:`~atlas.controllers.operations.TypedControllerOps`, and additionally
    exposes :meth:`run_agent` for the custom typed agents used by ranking,
    deep-fit review and application drafting.
    """

    name = "copilot"

    # Generic reasoning agent for the protocol methods (no private data by
    # default; JSON output validated by TypedControllerOps).
    _GENERIC_AGENT = CopilotAgentSpec(
        name="atlas-reasoner",
        prompt=(
            "You assist Atlas with a single typed reasoning step over compact "
            "structured input. Emit one strict JSON object. You never decide "
            "completion and never invent evidence."
        ),
        allowed_tools=(),
        timeout_s=45.0,
        retry_budget=1,
    )

    def __init__(
        self,
        transport: Optional[CopilotSdkTransport] = None,
        *,
        model: str = "claude-opus-4.8",
        max_session_credits: float = 50,
        session_timeout_s: float = 60.0,
        retry_budget: int = 1,
        allow_private_candidate: bool = False,
        account_type: str = "unspecified",
        permission_handler: Optional[LeastPrivilegePermissionHandler] = None,
        agents: Optional[Mapping[str, CopilotAgentSpec]] = None,
    ) -> None:
        self.transport: CopilotSdkTransport = transport or _OfficialCopilotSdkTransport()
        self.configured_model = model
        self.session_timeout_s = float(session_timeout_s)
        self.retry_budget = int(retry_budget)
        self.allow_private_candidate = bool(allow_private_candidate)
        self.account_type = account_type
        self.permissions = permission_handler or LeastPrivilegePermissionHandler()
        self.agents: dict[str, CopilotAgentSpec] = dict(agents or BUILTIN_AGENTS)
        # Ensure the generic protocol agent is always runnable.
        self.agents.setdefault(self._GENERIC_AGENT.name, self._GENERIC_AGENT)
        self.meter = UsageMeter(max_session_credits=max_session_credits)
        self._resolved_model: Optional[str] = None

    # -- consent ------------------------------------------------------------
    def private_data_allowed(self) -> bool:
        """BOTH the explicit flag AND a real account-type acknowledgement are
        required before any real candidate PII may reach a model."""
        return bool(self.allow_private_candidate) and self.account_type in ("personal", "organization")

    def assert_private_allowed(self) -> None:
        if not self.private_data_allowed():
            raise PrivateDataConsentError(
                "sending real candidate data to Copilot requires "
                "--allow-private-candidate-to-copilot AND a personal/organization "
                "account acknowledgement (copilot_account_type); refusing to send PII"
            )

    # -- model selection ----------------------------------------------------
    def select_model(self) -> str:
        """Prefer the configured model when the account exposes it, else the
        first available model. Cached for the controller's lifetime."""
        if self._resolved_model is not None:
            return self._resolved_model
        try:
            available = list(self.transport.available_models())
        except Exception:  # noqa: BLE001 - availability is best-effort
            available = []
        if not available:
            chosen = self.configured_model
        elif self.configured_model in available:
            chosen = self.configured_model
        else:
            chosen = available[0]
        self._resolved_model = chosen
        return chosen

    # -- core session -------------------------------------------------------
    def run_agent(
        self,
        agent_name: str,
        prompt_suffix: str,
        context: Optional[Mapping[str, Any]] = None,
        *,
        private: bool = False,
        resume_session_id: Optional[str] = None,
    ) -> CopilotSessionResult:
        """Run one bounded, permission-checked reasoning session for a custom
        agent. Returns a uniform :class:`CopilotSessionResult`; failures are
        quarantined (never trusted) instead of crashing the caller. Consent,
        permission, and budget VIOLATIONS raise loudly (they are policy errors,
        never silently ignored)."""
        agent = self.agents.get(agent_name)
        if agent is None:
            raise KeyError(f"unknown agent {agent_name!r}")

        # Consent gate: a private payload (or a private-by-nature agent) may only
        # run with explicit consent.
        if private or agent.handles_private_candidate:
            self.assert_private_allowed()

        # Least-privilege: prove every tool the agent may use is permitted. The
        # default-deny handler rejects shell/edit/git/network unconditionally.
        for tool in agent.allowed_tools:
            self.permissions.assert_allowed(tool, agent)

        model = self.select_model()
        session_id = resume_session_id or f"sess::{uuid.uuid4().hex[:16]}"
        full_prompt = f"{agent.prompt}\n\n{prompt_suffix}".strip()
        ctx = dict(context or {})

        attempts = agent.retry_budget + 1
        last_reason = ""
        for attempt in range(attempts):
            started = time.monotonic()
            try:
                raw = self.transport.run(
                    agent=agent,
                    prompt=full_prompt,
                    context=ctx,
                    model=model,
                    timeout_s=min(agent.timeout_s, self.session_timeout_s),
                    session_id=session_id,
                    resume=bool(resume_session_id),
                )
            except TimeoutError as exc:
                last_reason = f"timeout: {exc}"
                continue  # retryable within budget
            except CopilotSdkUnavailable as exc:
                # SDK not wired/installed: quarantine so the caller falls back
                # deterministically (this is the expected offline/disabled state).
                latency = (time.monotonic() - started) * 1000.0
                self.meter.record(agent=agent.name, model=model, session_id=session_id,
                                   input_tokens=0, output_tokens=0, credits=0.0,
                                   latency_ms=latency, outcome="SDK_UNAVAILABLE")
                return CopilotSessionResult(agent=agent.name, content="", model=model,
                                            session_id=session_id, latency_ms=latency,
                                            quarantined=True, quarantine_reason=str(exc),
                                            usage=self.meter.to_dict())
            except Exception as exc:  # noqa: BLE001 - any transport error is quarantined
                last_reason = f"transport error: {exc}"
                continue
            latency = (time.monotonic() - started) * 1000.0

            # Budget gate: fail closed BEFORE accepting output that would exceed
            # the bounded per-session credit ceiling.
            if self.meter.would_exceed(raw.credits):
                self.meter.record(agent=agent.name, model=model, session_id=session_id,
                                   input_tokens=raw.input_tokens, output_tokens=raw.output_tokens,
                                   credits=0.0, latency_ms=latency, outcome="BUDGET_DENIED")
                raise SessionBudgetExceeded(
                    f"session would exceed credit budget "
                    f"({self.meter.credits}+{raw.credits} > {self.meter.max_session_credits})"
                )

            content = _strip_code_fences((raw.content or "").strip())
            outcome = "OK" if content else "EMPTY"
            self.meter.record(agent=agent.name, model=model, session_id=raw.session_id or session_id,
                              input_tokens=raw.input_tokens, output_tokens=raw.output_tokens,
                              credits=raw.credits, latency_ms=latency, outcome=outcome)
            return CopilotSessionResult(
                agent=agent.name, content=content, model=model,
                session_id=raw.session_id or session_id, latency_ms=latency,
                quarantined=not content, quarantine_reason="" if content else "empty content",
                usage=self.meter.to_dict(),
            )

        # Exhausted retries.
        self.meter.record(agent=agent.name, model=model, session_id=session_id,
                          input_tokens=0, output_tokens=0, credits=0.0,
                          latency_ms=0.0, outcome="QUARANTINED")
        return CopilotSessionResult(agent=agent.name, content="", model=model,
                                    session_id=session_id, latency_ms=0.0,
                                    quarantined=True, quarantine_reason=last_reason or "exhausted retries",
                                    usage=self.meter.to_dict())

    def usage(self) -> dict[str, Any]:
        return self.meter.to_dict()

    # -- Controller protocol ------------------------------------------------
    def _protocol_call(self, op: str, request: ControllerRequest) -> ControllerResponse:
        """Adapt a generic protocol op to a quarantine-safe agent run. Returns
        empty content on any failure so :class:`TypedControllerOps` falls back
        deterministically instead of raising."""
        ctx = dict(request.context or {})
        result = self.run_agent(self._GENERIC_AGENT.name, request.prompt, ctx)
        return ControllerResponse(
            content=result.content,
            raw=None,
            metadata={
                "controller": self.name,
                "operation": op,
                "model": result.model,
                "session_id": result.session_id,
                "quarantined": result.quarantined,
                "latency_ms": result.latency_ms,
            },
        )

    def reason(self, request: ControllerRequest) -> ControllerResponse:
        return self._protocol_call("reason", request)

    def classify(self, request: ControllerRequest) -> ControllerResponse:
        return self._protocol_call("classify", request)

    def extract(self, request: ControllerRequest) -> ControllerResponse:
        return self._protocol_call("extract", request)

    def review(self, request: ControllerRequest) -> ControllerResponse:
        return self._protocol_call("review", request)


# Backwards-compatible alias: earlier code referenced ``CopilotController``.
CopilotController = CopilotSdkController


def build_copilot_controller(settings: Any, *, transport: Optional[CopilotSdkTransport] = None) -> CopilotSdkController:
    """Construct a :class:`CopilotSdkController` from Atlas settings. When no
    transport is supplied, the lazily-bound official-SDK transport is used (it
    imports the SDK only on first real call)."""
    return CopilotSdkController(
        transport=transport,
        model=getattr(settings, "controller_model", "claude-opus-4.8"),
        max_session_credits=getattr(settings, "controller_max_session_credits", 50),
        session_timeout_s=getattr(settings, "controller_session_timeout_s", 60.0),
        retry_budget=getattr(settings, "controller_retry_budget", 1),
        allow_private_candidate=getattr(settings, "allow_private_candidate_to_copilot", False),
        account_type=getattr(settings, "copilot_account_type", "unspecified"),
    )


__all__ = [
    "OFFICIAL_SDK_PACKAGE",
    "FORBIDDEN_TOOLS",
    "LOCAL_READONLY_TOOLS",
    "CopilotSdkUnavailable",
    "PrivateDataConsentError",
    "SessionBudgetExceeded",
    "PermissionDeniedError",
    "RawSdkResult",
    "CopilotSdkTransport",
    "CopilotAgentSpec",
    "BUILTIN_AGENTS",
    "PermissionDecision",
    "LeastPrivilegePermissionHandler",
    "UsageMeter",
    "CopilotSessionResult",
    "CopilotSdkController",
    "CopilotController",
    "build_copilot_controller",
]
