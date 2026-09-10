"""The CLI-Playwright company-search backend implementation.

Orchestrates: MCP config -> prompt -> one bounded Copilot process -> JSONL parse
-> non-bypassable validation -> typed :class:`BackendResult`. Invalid agent
output is quarantined; internal failures are classified as retryable (never
terminal). No auto-apply, no login.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from atlas.browser_backend.jsonl_parser import count_tool_calls, extract_result_objects
from atlas.browser_backend.mcp_config import build_mcp_config, write_mcp_config
from atlas.browser_backend.models import (
    BackendResult, CliProcessConfig, CompanyTask, ROUTE_CLI_PLAYWRIGHT,
)
from atlas.browser_backend.prompt_builder import build_company_prompt
from atlas.browser_backend.validation import build_validated_result, validate_result_object
from atlas.pilot.status_v4 import CompanySearchStatus


def parse_usage_credits(usage_path: Path) -> float:
    """Best-effort AI-credit total from the Copilot ``--usage-output-file`` JSON."""
    if not usage_path.exists():
        return 0.0
    try:
        data = json.loads(usage_path.read_text(encoding="utf-8", errors="replace"))
    except (ValueError, OSError):
        return 0.0

    def _walk(obj) -> float:
        total = 0.0
        if isinstance(obj, dict):
            for k, v in obj.items():
                kl = str(k).lower()
                if isinstance(v, (int, float)):
                    if "nano_aiu" in kl or "nanoaiu" in kl:
                        total += float(v) / 1e9
                    elif kl in ("ai_credits", "credits", "total_credits"):
                        total += float(v)
                else:
                    total += _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                total += _walk(item)
        return total

    return round(_walk(data), 4)


class CliPlaywrightBackend:
    """A :class:`atlas.browser_backend.protocol.CompanySearchBackend`."""

    name = "cli_playwright"

    def __init__(
        self,
        output_root: Path,
        *,
        process_config: Optional[CliProcessConfig] = None,
        agent: str = "atlas-company-search-cli",
        candidate_max_years: Optional[float] = None,
        copilot_path: Optional[Path] = None,
        recipe: Optional[dict] = None,
    ):
        self.output_root = Path(output_root)
        self.process_config = process_config or CliProcessConfig()
        self.agent = agent
        self.candidate_max_years = candidate_max_years
        self.copilot_path = copilot_path
        self.recipe = recipe

    def _task_dir(self, task: CompanyTask) -> Path:
        safe = "".join(c for c in task.company if c.isalnum() or c in ("-", "_")) or "company"
        return (self.output_root / f"{safe}-{task.task_id[:8]}").resolve()

    def search_company(self, task: CompanyTask) -> BackendResult:
        from atlas.browser_backend.cli_process import run_company_process

        out_dir = self._task_dir(task)
        out_dir.mkdir(parents=True, exist_ok=True)
        mcp_cfg = build_mcp_config(task, out_dir, headed=task.headed)
        mcp_cfg_path = write_mcp_config(mcp_cfg, out_dir / "mcp-config.json")
        prompt = build_company_prompt(task, recipe=self.recipe)

        cap = run_company_process(
            task, self.process_config, prompt=prompt, mcp_config_path=mcp_cfg_path,
            output_dir=out_dir, copilot_path=self.copilot_path, agent=self.agent,
            add_dir=Path(__file__).resolve().parents[2],  # repo root => discover .github skills/agents
        )

        result = BackendResult(task=task, route=ROUTE_CLI_PLAYWRIGHT,
                               status=CompanySearchStatus.BROWSER_TOOL_ERROR.value, capture=cap)
        result.usage_credits = parse_usage_credits(Path(cap.usage_path))

        # Internal failure: process could not complete cleanly.
        if cap.exit_code not in (0,) or cap.timed_out or cap.interrupted:
            result.status = CompanySearchStatus.ASYNC_RUNTIME_ERROR.value if cap.timed_out \
                else CompanySearchStatus.BROWSER_TOOL_ERROR.value
            result.error = (f"process exit={cap.exit_code} timed_out={cap.timed_out} "
                            f"interrupted={cap.interrupted}")
            # Still try to parse a result object for evidence, but do not accept it.
            return self._finalize_invalid(task, cap, result, out_dir)

        objs = extract_result_objects(cap.final_assistant_text)
        if len(objs) == 0:
            # Fallback: parse the raw stdout directly (covers text-mode output or
            # a JSONL schema our event parser did not recognize).
            try:
                raw = Path(cap.stdout_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                raw = ""
            if raw:
                objs = extract_result_objects(raw)
                if raw.strip() and not cap.final_assistant_text:
                    cap.final_assistant_text = raw[-20000:]
        if len(objs) == 0:
            result.status = CompanySearchStatus.PARSER_ERROR.value
            result.error = "no machine-readable result object in final message"
            return self._finalize_invalid(task, cap, result, out_dir)
        if len(objs) > 1:
            result.status = CompanySearchStatus.PARSER_ERROR.value
            result.error = f"expected exactly one result object, found {len(objs)}"
            result.proposed = objs[0]
            return self._finalize_invalid(task, cap, result, out_dir)

        proposed = objs[0]
        result.proposed = proposed
        contract = validate_result_object(proposed, task, custom_site=True)
        result.browser_evidence = contract["browser_evidence"]
        result.external_block = contract["external_block"]
        if contract["failures"]:
            result.status = CompanySearchStatus.PARSER_ERROR.value
            result.validation_failures = contract["failures"]
            result.valid = False
            return self._finalize_invalid(task, cap, result, out_dir)

        validated = build_validated_result(proposed, task, candidate_max_years=self.candidate_max_years)
        result.result = validated
        result.status = validated.status
        result.valid = True
        result.details_opened = len(validated.evidence_urls) or len(validated.jobs)
        result.recipe_candidate = proposed.get("recipe_candidate")
        (out_dir / "backend_result.json").write_bytes(
            json.dumps(result.to_dict(), indent=2, ensure_ascii=False).encode("utf-8"))
        return result

    def _finalize_invalid(self, task: CompanyTask, cap, result: BackendResult, out_dir: Path) -> BackendResult:
        """Quarantine invalid/partial output and persist the audit trail."""
        q = out_dir / "quarantine"
        q.mkdir(parents=True, exist_ok=True)
        payload = {
            "company": task.company, "task_id": task.task_id, "run_id": task.run_id,
            "status": result.status, "error": result.error,
            "validation_failures": result.validation_failures,
            "final_assistant_text": (cap.final_assistant_text or "")[:20000],
            "proposed": result.proposed,
            "exit_code": cap.exit_code, "timed_out": cap.timed_out,
        }
        qpath = q / "quarantine.json"
        qpath.write_bytes(json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"))
        result.quarantine_path = str(qpath)
        (out_dir / "backend_result.json").write_bytes(
            json.dumps(result.to_dict(), indent=2, ensure_ascii=False).encode("utf-8"))
        return result


__all__ = ["CliPlaywrightBackend", "parse_usage_credits"]
