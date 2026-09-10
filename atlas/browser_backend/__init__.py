"""Atlas Copilot-CLI + Playwright-MCP browser backend (local-v2 integration).

Reusable production browser backend for custom / difficult official career
sites, integrating the strategy proven in the manual Accenture canary:

    Copilot CLI  +  Microsoft Playwright MCP  +  DOM canonical-href extraction
    +  direct job-detail navigation  +  deterministic Atlas validation.

Responsibilities are kept deliberately separate (one module each): protocol,
models, mcp_config, prompt_builder, jsonl_parser, cli_process, cli_playwright,
hybrid, recipes, validation, review_queue, install, doctor.

LangGraph and the existing company-task boundary talk only to
:class:`CompanySearchBackend`; subprocess/MCP details never leak upward. The
legacy V4 SDK/custom-browser actor (``atlas.pilot.browser_actor``) is now
explicitly experimental/legacy and is routed around, not deleted.
"""

from __future__ import annotations

MCP_PACKAGE = "@playwright/mcp"
MCP_VERSION = "0.0.80"
MCP_LICENSE = "Apache-2.0"

# Default product install root (Windows). The launcher may override via
# ATLAS_PLAYWRIGHT_MCP_ROOT / ATLAS_PLAYWRIGHT_MCP_CLI.
DEFAULT_INSTALL_SUBPATH = ("Atlas", "tools", "playwright-mcp", MCP_VERSION)

__all__ = ["MCP_PACKAGE", "MCP_VERSION", "MCP_LICENSE", "DEFAULT_INSTALL_SUBPATH"]
