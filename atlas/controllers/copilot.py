"""Placeholder Copilot controller adapter.

This module intentionally does NOT implement a working integration. There
is no supported, non-hacky way to programmatically invoke GitHub Copilot
from inside a plain Python runtime process today, and Atlas must not
attempt unsupported hacks to do so (see project rules).

When a supported integration strategy becomes available (for example, an
official SDK/API), implement it here WITHOUT changing the Controller
protocol or any deterministic orchestration code that depends on it.
"""

from __future__ import annotations

from atlas.controllers.base import BaseController, ControllerRequest, ControllerResponse


class CopilotController(BaseController):
    """Reserved adapter slot for a future, officially-supported Copilot
    integration. Every method raises NotImplementedError until then."""

    name = "copilot"

    _NOT_IMPLEMENTED_MESSAGE = (
        "CopilotController is a reserved placeholder. No supported "
        "programmatic Copilot integration is wired up yet. Use "
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
