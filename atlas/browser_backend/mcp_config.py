"""Generate one Playwright-MCP configuration file per company task.

The file is written as **UTF-8 without BOM** (Copilot CLI rejects a BOM), with an
isolated browser context, a unique output directory, and bounded timeouts. A
persistent browser profile is *never* shared between concurrent companies.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from atlas.browser_backend.models import CompanyTask, MCPConfig


def resolve_cli_path(explicit: Optional[str] = None) -> str:
    """Resolve the absolute Playwright-MCP ``cli.js``.

    Preference order: explicit arg -> ``ATLAS_PLAYWRIGHT_MCP_CLI`` ->
    ``ATLAS_PLAYWRIGHT_MCP_ROOT`` -> default product install root.
    """
    if explicit:
        return str(Path(explicit))
    env_cli = os.environ.get("ATLAS_PLAYWRIGHT_MCP_CLI")
    if env_cli:
        return str(Path(env_cli))
    root = os.environ.get("ATLAS_PLAYWRIGHT_MCP_ROOT")
    if root:
        return str(Path(root) / "node_modules" / "@playwright" / "mcp" / "cli.js")
    from atlas.browser_backend import DEFAULT_INSTALL_SUBPATH

    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    return str(base.joinpath(*DEFAULT_INSTALL_SUBPATH, "node_modules", "@playwright", "mcp", "cli.js"))


def build_mcp_config(
    task: CompanyTask,
    output_dir: Path,
    *,
    cli_path: Optional[str] = None,
    browser_channel: Optional[str] = None,
    headed: bool = False,
) -> MCPConfig:
    channel = browser_channel or os.environ.get("ATLAS_BROWSER_CHANNEL", "chrome")
    ctx_dir = output_dir / "browser-context"
    return MCPConfig(
        cli_path=resolve_cli_path(cli_path),
        browser_channel=channel,
        output_dir=str(output_dir / "mcp-output"),
        headless=not (headed or task.headed),
        user_data_dir=str(ctx_dir),  # unique per task => isolated context
    )


def write_mcp_config(config: MCPConfig, path: Path) -> Path:
    """Serialize ``config`` to ``path`` as UTF-8 **without** a BOM."""
    path.parent.mkdir(parents=True, exist_ok=True)
    Path(config.output_dir or "").parent.mkdir(parents=True, exist_ok=True) if config.output_dir else None
    data = json.dumps(config.to_config_dict(), indent=2, ensure_ascii=False)
    # Explicitly encode without BOM (utf-8, never utf-8-sig).
    path.write_bytes(data.encode("utf-8"))
    return path


__all__ = ["resolve_cli_path", "build_mcp_config", "write_mcp_config"]
