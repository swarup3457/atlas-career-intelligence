"""Production routing boundary + minimal run policy.

    structured public ATS/API resolved?
      yes -> existing structured backend (Copilot is bypassed)
      no  -> Copilot CLI Playwright backend

The legacy V4 SDK/custom-browser actor is available only as an explicit
``legacy_v4`` route (experimental), never the default. Company-agent concurrency
is capped at two, each with isolated MCP/browser state (separate task dirs).
Completed companies persist immediately and are skipped on resume.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Optional

from atlas.browser_backend.models import (
    BackendResult, CompanyTask, ROUTE_CLI_PLAYWRIGHT, ROUTE_LEGACY_V4, ROUTE_STRUCTURED,
)
from atlas.browser_backend.protocol import CompanySearchBackend
from atlas.browser_backend.review_queue import append_review_item, should_enqueue
from atlas.pilot.status_v4 import is_internal_retryable, is_terminal

MAX_COMPANY_CONCURRENCY = 2


class CompletionLedger:
    """Append-only per-run completion ledger so resume skips completed work."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def mark(self, run_id: str, company: str, status: str, *, valid: bool = False) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = {"run_id": run_id, "company": company, "status": status, "valid": valid,
               "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def completed(self, run_id: str) -> dict:
        """Return ``{company: status}`` for companies terminal in this run."""
        out: dict = {}
        if not self.path.exists():
            return out
        for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            if row.get("run_id") == run_id and is_terminal(str(row.get("status", ""))):
                out[row.get("company")] = row.get("status")
        return out

    def is_completed(self, run_id: str, company: str) -> bool:
        return company in self.completed(run_id)


class HybridRouter:
    """Route one company to the structured or CLI-Playwright backend."""

    def __init__(
        self,
        cli_backend: CompanySearchBackend,
        *,
        structured_backend: Optional[CompanySearchBackend] = None,
        legacy_backend: Optional[CompanySearchBackend] = None,
        structured_resolver: Optional[Callable[[CompanyTask], bool]] = None,
    ):
        self.cli_backend = cli_backend
        self.structured_backend = structured_backend
        self.legacy_backend = legacy_backend
        self.structured_resolver = structured_resolver

    def resolve_route(self, task: CompanyTask) -> str:
        if self.structured_backend is not None and self.structured_resolver is not None:
            try:
                if self.structured_resolver(task):
                    return ROUTE_STRUCTURED
            except Exception:
                pass
        return ROUTE_CLI_PLAYWRIGHT

    def search_company(self, task: CompanyTask, *, route: Optional[str] = None) -> BackendResult:
        route = route or self.resolve_route(task)
        if route == ROUTE_STRUCTURED and self.structured_backend is not None:
            res = self.structured_backend.search_company(task)
            res.route = ROUTE_STRUCTURED
            return res
        if route == ROUTE_LEGACY_V4:
            if self.legacy_backend is None:
                raise ValueError("legacy_v4 route requested but no legacy backend registered")
            res = self.legacy_backend.search_company(task)
            res.route = ROUTE_LEGACY_V4
            return res
        return self.cli_backend.search_company(task)


def run_company_with_policy(
    router: HybridRouter,
    task: CompanyTask,
    *,
    ledger: Optional[CompletionLedger] = None,
    review_queue_path: Optional[Path] = None,
    max_internal_retries: int = 1,
) -> BackendResult:
    """Run one company with the run policy: external block is terminal; internal
    failures retry the SAME company at most ``max_internal_retries`` times, then
    enter the review queue. Completed companies are skipped on resume."""
    if ledger is not None and ledger.is_completed(task.run_id, task.company):
        res = BackendResult(task=task, route="skipped", status=ledger.completed(task.run_id)[task.company])
        res.error = "skipped: already completed in this run"
        return res

    attempts = 0
    result = router.search_company(task)
    while is_internal_retryable(result.status) and attempts < max_internal_retries:
        attempts += 1
        result = router.search_company(task)

    if should_enqueue(result.status, error=result.error):
        cap = result.capture
        append_review_item(
            result, queue_path=review_queue_path, error=result.error,
            last_state=result.status,
            transcript_path=getattr(cap, "stdout_path", "") if cap else "",
            usage_path=getattr(cap, "usage_path", "") if cap else "",
            mcp_output_dir=getattr(cap, "mcp_output_dir", "") if cap else "",
            retry_count=attempts,
        )

    if ledger is not None and is_terminal(result.status):
        ledger.mark(task.run_id, task.company, result.status, valid=result.valid)
    return result


__all__ = [
    "MAX_COMPANY_CONCURRENCY", "CompletionLedger", "HybridRouter", "run_company_with_policy",
]
