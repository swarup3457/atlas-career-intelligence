"""Typed dataclasses for the CLI-Playwright browser backend.

Decoupled from LangGraph and from the subprocess layer. The job payload reuses
the existing pilot contracts (:mod:`atlas.pilot.models`); everything here is
backend plumbing + the machine-readable result envelope the company agent emits.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

RESULT_VERSION = 1

#: Route labels for the hybrid boundary.
ROUTE_STRUCTURED = "structured_ats"
ROUTE_CLI_PLAYWRIGHT = "cli_playwright"
ROUTE_LEGACY_V4 = "legacy_v4"

#: Observed real result states (a card list is *not* one of these on its own).
OBSERVED_RESULTS = "results_observed"
OBSERVED_NO_RESULTS = "no_results"
OBSERVED_EXTERNAL_BLOCK = "external_block"
OBSERVED_STATES = frozenset({OBSERVED_RESULTS, OBSERVED_NO_RESULTS, OBSERVED_EXTERNAL_BLOCK})


@dataclass(frozen=True)
class CompanyTask:
    """One company to search — the unit the backend owns end to end."""

    company: str
    official_domain: str = ""
    career_entry_url: str = ""
    run_id: str = "browser-backend"
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    query_terms: tuple[str, ...] = ()
    locations: tuple[str, ...] = ("India",)
    lanes: tuple[str, ...] = ()
    headed: bool = False
    debug: bool = False

    @property
    def session_name(self) -> str:
        safe = "".join(c for c in self.company if c.isalnum() or c in ("-", "_")) or "company"
        return f"atlas-{safe}-{self.task_id[:8]}"


@dataclass
class MCPConfig:
    """Resolved inputs for one Playwright-MCP configuration file."""

    cli_path: str
    browser_channel: str = "chrome"
    output_dir: str = ""
    headless: bool = True
    save_session: bool = True
    snapshot_mode: str = "full"
    image_responses: str = "omit"
    action_timeout_ms: int = 10000
    navigation_timeout_ms: int = 90000
    settle_timeout_ms: int = 1000
    isolated: bool = True
    user_data_dir: str = ""  # isolated per task; never a shared persistent profile

    def to_config_dict(self) -> dict:
        """Render the ``mcpServers`` config consumed by Copilot CLI.

        The argument list is built here (never a shell string) and points at the
        absolute MCP ``cli.js``. Normal runs are headless with image responses
        omitted; explicit canaries/debug flip ``headless``.
        """
        args = [
            self.cli_path,
            "--browser", self.browser_channel,
            "--isolated" if self.isolated else "--no-isolated",
            "--save-session" if self.save_session else "--no-save-session",
            "--snapshot-mode", self.snapshot_mode,
            "--image-responses", self.image_responses,
            "--timeout-action", str(self.action_timeout_ms),
            "--timeout-navigation", str(self.navigation_timeout_ms),
        ]
        if self.output_dir:
            args += ["--output-dir", self.output_dir]
        if self.user_data_dir:
            args += ["--user-data-dir", self.user_data_dir]
        args.append("--headless" if self.headless else "--no-headless")
        return {
            "mcpServers": {
                "playwright": {
                    "type": "local",
                    "command": "node",
                    "args": args,
                    "tools": ["*"],
                }
            }
        }


@dataclass
class CliProcessConfig:
    """Bounded configuration for one Copilot-CLI company process."""

    model: str = "claude-sonnet-5"
    max_ai_credits: int = 250
    max_continuations: int = 8
    wall_clock_timeout_s: int = 12 * 60
    long_context: bool = False
    reasoning_effort: str = ""  # empty => do not force
    allow_all_mcp_tools: bool = True
    no_ask_user: bool = True
    no_remote_export: bool = True


@dataclass
class ProcessCapture:
    """Everything captured from one Copilot-CLI child process."""

    argv: list[str] = field(default_factory=list)
    exit_code: Optional[int] = None
    started_at: float = 0.0
    ended_at: float = 0.0
    timed_out: bool = False
    interrupted: bool = False
    stdout_path: str = ""
    stderr_path: str = ""
    usage_path: str = ""
    mcp_output_dir: str = ""
    jsonl_events: list[dict] = field(default_factory=list)
    final_assistant_text: str = ""

    @property
    def elapsed_s(self) -> float:
        if self.started_at and self.ended_at:
            return round(self.ended_at - self.started_at, 3)
        return 0.0


@dataclass
class BackendResult:
    """The backend's own return value: the validated company result plus audit.

    ``result`` is the deterministically-validated
    :class:`atlas.pilot.models.CompanySearchResult`; ``proposed`` is the raw
    (untrusted) object the agent emitted, retained for audit/quarantine.
    """

    task: CompanyTask
    route: str
    status: str
    result: object = None  # CompanySearchResult once validated
    proposed: Optional[dict] = None
    valid: bool = False
    validation_failures: list[str] = field(default_factory=list)
    browser_evidence: bool = False
    external_block: bool = False
    quarantine_path: str = ""
    capture: Optional[ProcessCapture] = None
    usage_credits: float = 0.0
    details_opened: int = 0
    recipe_candidate: Optional[dict] = None
    error: str = ""

    def to_dict(self) -> dict:
        d = {
            "company": self.task.company,
            "task_id": self.task.task_id,
            "run_id": self.task.run_id,
            "route": self.route,
            "status": self.status,
            "valid": self.valid,
            "browser_evidence": self.browser_evidence,
            "external_block": self.external_block,
            "details_opened": self.details_opened,
            "usage_credits": round(self.usage_credits, 4),
            "validation_failures": list(self.validation_failures),
            "error": self.error,
        }
        if self.result is not None and hasattr(self.result, "to_dict"):
            d["result"] = self.result.to_dict()
        if self.quarantine_path:
            d["quarantine_path"] = self.quarantine_path
        if self.recipe_candidate:
            d["recipe_candidate"] = self.recipe_candidate
        return d


def new_run_id(prefix: str = "bb") -> str:
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


__all__ = [
    "RESULT_VERSION", "ROUTE_STRUCTURED", "ROUTE_CLI_PLAYWRIGHT", "ROUTE_LEGACY_V4",
    "OBSERVED_RESULTS", "OBSERVED_NO_RESULTS", "OBSERVED_EXTERNAL_BLOCK", "OBSERVED_STATES",
    "CompanyTask", "MCPConfig", "CliProcessConfig", "ProcessCapture", "BackendResult", "new_run_id",
]
