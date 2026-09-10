"""Backend readiness diagnostics (``atlas browser-backend doctor``).

Pure inspection — no browser launch, no network. Reports Node, the pinned MCP
CLI (path/version/license), the launcher env vars, and Copilot-CLI availability.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from atlas.browser_backend import MCP_LICENSE, MCP_VERSION
from atlas.browser_backend.install import cli_version, node_ok, _installed_license
from atlas.browser_backend.mcp_config import resolve_cli_path

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass
class DoctorReport:
    checks: list[dict] = field(default_factory=list)

    def add(self, name: str, level: str, detail: str = "") -> None:
        self.checks.append({"check": name, "level": level, "detail": detail})

    @property
    def overall(self) -> str:
        levels = [c["level"] for c in self.checks]
        if FAIL in levels:
            return FAIL
        if WARN in levels:
            return WARN
        return PASS

    def to_dict(self) -> dict:
        return {"overall": self.overall, "checks": self.checks}

    def render(self) -> str:
        lines = [f"[{c['level']}] {c['check']}" + (f" - {c['detail']}" if c["detail"] else "")
                 for c in self.checks]
        lines.append(f"OVERALL: {self.overall}")
        return "\n".join(lines)


def run_doctor() -> DoctorReport:
    rep = DoctorReport()

    ok, node_msg = node_ok()
    rep.add("Node >= 18", PASS if ok else FAIL, node_msg)

    cli_path = Path(resolve_cli_path())
    if cli_path.exists():
        ver = cli_version(cli_path)
        rep.add("Playwright MCP cli.js present", PASS, str(cli_path))
        rep.add("Playwright MCP version pinned", PASS if ver == MCP_VERSION else FAIL,
                f"resolved={ver} expected={MCP_VERSION}")
        lic = _installed_license(cli_path.parents[3]) if len(cli_path.parents) >= 4 else ""
        rep.add("Playwright MCP license", PASS if MCP_LICENSE.lower() in (lic or "").lower() else WARN,
                f"{lic or 'unknown'} (expected {MCP_LICENSE})")
    else:
        rep.add("Playwright MCP cli.js present", FAIL,
                f"not found at {cli_path} (run `atlas browser-backend install`)")

    for var in ("ATLAS_PLAYWRIGHT_MCP_CLI", "ATLAS_PLAYWRIGHT_MCP_ROOT", "ATLAS_BROWSER_CHANNEL"):
        val = os.environ.get(var, "")
        rep.add(f"env {var}", PASS if val else WARN, val or "(unset — defaults used)")

    copilot = shutil.which("copilot") or shutil.which("copilot.cmd")
    rep.add("Copilot CLI on PATH", PASS if copilot else WARN,
            copilot or "(not found — live canaries unavailable)")

    return rep


__all__ = ["DoctorReport", "run_doctor", "PASS", "WARN", "FAIL"]
