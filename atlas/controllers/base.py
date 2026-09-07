"""Atlas controller abstraction — provider-neutral LLM reasoning interface.

Deterministic orchestration (LangGraph, retries, checkpoints, browser
workers, SQLite state) MUST work correctly with ``controller=None`` and
must never depend on any specific LLM product to function. This module
defines the interface that a future reasoning controller (Copilot, Codex,
or another supported product) will implement, without baking any
product-specific behavior into the rest of the codebase.

Nothing in this module calls out to a real LLM. Concrete adapters live in
sibling modules (copilot.py, codex.py) and are intentionally left as
documented placeholders until a supported integration strategy exists.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable


@dataclass
class ControllerRequest:
    """Generic payload passed to a controller operation."""

    prompt: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class ControllerResponse:
    """Generic result returned by a controller operation."""

    content: str
    raw: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Controller(Protocol):
    """Protocol every Atlas controller adapter must satisfy.

    Deterministic workers should type-hint against this Protocol (or the
    ABC below), never against a concrete adapter class, so the runtime
    integration strategy can be swapped later without touching worker
    code.
    """

    name: str

    def reason(self, request: ControllerRequest) -> ControllerResponse:
        """General-purpose reasoning/decision-making call."""
        ...

    def classify(self, request: ControllerRequest) -> ControllerResponse:
        """Classify input into one of a set of provided categories."""
        ...

    def extract(self, request: ControllerRequest) -> ControllerResponse:
        """Extract structured information from unstructured input."""
        ...

    def review(self, request: ControllerRequest) -> ControllerResponse:
        """Review/critique a proposed result for correctness."""
        ...


class BaseController(ABC):
    """Convenience ABC implementing the Controller protocol."""

    name: str = "base"

    @abstractmethod
    def reason(self, request: ControllerRequest) -> ControllerResponse: ...

    @abstractmethod
    def classify(self, request: ControllerRequest) -> ControllerResponse: ...

    @abstractmethod
    def extract(self, request: ControllerRequest) -> ControllerResponse: ...

    @abstractmethod
    def review(self, request: ControllerRequest) -> ControllerResponse: ...


class NullController(BaseController):
    """Deterministic, no-LLM controller used for tests and pure-code runs.

    Every method returns a clearly-marked placeholder response rather than
    raising, so deterministic pipelines that only *optionally* consult a
    controller can run end-to-end with ``controller=none`` and no LLM
    involved at all.
    """

    name = "none"

    def _respond(self, op: str, request: ControllerRequest) -> ControllerResponse:
        return ControllerResponse(
            content="",
            raw=None,
            metadata={"controller": self.name, "operation": op, "noop": True},
        )

    def reason(self, request: ControllerRequest) -> ControllerResponse:
        return self._respond("reason", request)

    def classify(self, request: ControllerRequest) -> ControllerResponse:
        return self._respond("classify", request)

    def extract(self, request: ControllerRequest) -> ControllerResponse:
        return self._respond("extract", request)

    def review(self, request: ControllerRequest) -> ControllerResponse:
        return self._respond("review", request)


def get_controller(name: str) -> Controller:
    """Controller factory. Isolates ALL product-specific imports/wiring.

    Supported values today: "none" (NullController). "copilot" and
    "codex" are registered but resolve to documented NotImplementedError
    placeholders until a supported runtime integration strategy exists
    (see docs/CONTROLLER_ABSTRACTION.md).
    """
    normalized = (name or "none").strip().lower()
    if normalized in ("none", "null", ""):
        return NullController()
    if normalized == "copilot":
        from atlas.controllers.copilot import CopilotController

        return CopilotController()
    if normalized == "codex":
        from atlas.controllers.codex import CodexController

        return CodexController()
    raise ValueError(
        f"Unknown controller '{name}'. Supported: none, copilot, codex."
    )
