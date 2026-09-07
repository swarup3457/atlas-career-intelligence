"""Placeholder Codex controller adapter.

Mirrors atlas/controllers/copilot.py: a reserved, isolated slot for a
future officially-supported Codex integration. No unsupported hacks, no
paid external API keys required by default, and no behavior baked into
deterministic orchestration code.
"""

from __future__ import annotations

from atlas.controllers.base import BaseController, ControllerRequest, ControllerResponse


class CodexController(BaseController):
    """Reserved adapter slot for a future, officially-supported Codex
    integration. Every method raises NotImplementedError until then."""

    name = "codex"

    _NOT_IMPLEMENTED_MESSAGE = (
        "CodexController is a reserved placeholder. No supported "
        "programmatic Codex integration is wired up yet. Use "
        "controller='none' for deterministic runs, or supply a concrete "
        "implementation here once an official integration path exists."
    )

    def reason(self, request: ControllerRequest) -> ControllerResponse:
        raise NotImplementedError(self._NOT_IMPLEMENTED_MESSAGE)

    def classify(self, request: ControllerRequest) -> ControllerResponse:
        raise NotImplementedError(self._NOT_IMPLEMENTED_MESSAGE)

    def extract(self, request: ControllerRequest) -> ControllerResponse:
        raise NotImplementedError(self._NOT_IMPLEMENTED_MESSAGE)

    def review(self, request: ControllerRequest) -> ControllerResponse:
        raise NotImplementedError(self._NOT_IMPLEMENTED_MESSAGE)
