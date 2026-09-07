"""Atlas controllers package — provider-neutral LLM reasoning adapters.

Import Controller/ControllerRequest/ControllerResponse/get_controller from
here for convenience; concrete adapters stay isolated in their own
modules (copilot.py, codex.py) so product-specific code never leaks into
deterministic orchestration.
"""

from atlas.controllers.base import (
    BaseController,
    Controller,
    ControllerRequest,
    ControllerResponse,
    NullController,
    get_controller,
)

__all__ = [
    "BaseController",
    "Controller",
    "ControllerRequest",
    "ControllerResponse",
    "NullController",
    "get_controller",
]
