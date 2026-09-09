"""The LLM company-search worker (architecture s.4/5).

One worker searches ONE company. In *live* mode it drives a real, tool-using Copilot SDK
session (default model ``claude-sonnet-5``) that is granted ONLY the six constrained Atlas
tools (``available_tools=[]`` denies every built-in shell/edit/git/network tool) and a
default-deny permission handler — so the model can direct the search but can never widen the
safety envelope. Usage events are captured into the :class:`UsageMeter`.

In *deterministic* mode (tests / NullController / when the SDK is unavailable) the SAME
toolbox is driven by Python through the identical tool surface, so the pipeline is fully
exercisable offline and always records the model that actually ran. Neither mode ever lets a
company be "answered from memory": a result with no tool calls is impossible to submit.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

from atlas.pilot.config import PilotConfig
from atlas.pilot.models import CompanySearchResult, CompanyStatus
from atlas.pilot.tools import CompanySearchToolbox, build_sdk_tools
from atlas.pilot.usage import UsageMeter

__all__ = ["LlmCompanySearchWorker", "CompanyTask", "build_company_prompt"]


@dataclass
class CompanyTask:
    company: str
    task_id: str
    domain_hint: str = ""
    entry_hint: str = ""
    india_only: bool = True
    feedback: str = ""          # exact missing checklist for a same-company retry
    escalate: bool = False


def build_company_prompt(task: CompanyTask, config: PilotConfig, profile_summary: dict) -> str:
    lanes_block = []
    for lane in config.primary_lanes:
        qs = ", ".join(config.query_for(lane))
        lanes_block.append(f"  - {lane}: {qs}")
    lanes_text = "\n".join(lanes_block)
    locs = ", ".join(config.location_variants) or "India, Bengaluru, Hyderabad, Pune, Chennai, Mumbai, Remote India"
    fb = f"\nRETRY FEEDBACK (address exactly): {task.feedback}\n" if task.feedback else ""
    return (
        f"You are Atlas's official-career search agent for ONE company: {task.company}.\n"
        f"Search ONLY the company's OFFICIAL career site through the provided tools. You MUST use the "
        f"tools for every fact; never answer from memory. Read-only only: never apply, log in, or bypass "
        f"any challenge.\n\n"
        f"Workflow:\n"
        f"1. resolve_official_company_site (domain hint: {task.domain_hint or 'unknown'}).\n"
        f"2. discover_official_careers_entry to fetch the official careers entry and detect the ATS/route.\n"
        f"3. For EACH of the five lanes below, run search_official_career_site with 1-2 short query variants, "
        f"restricted to India locations ({locs}).\n"
        f"4. open_official_job_detail for the most plausible India roles (bounded).\n"
        f"5. If the site is a dynamic SPA with no public API, try ONE bounded browser_interact_career_search.\n"
        f"6. Finally call submit_company_search_result. Always submit, even for a truthful blocker "
        f"(ACCESS_LIMITED / AUTH_REQUIRED / OFFICIAL_SOURCE_UNRESOLVED / UNSUPPORTED_SITE).\n\n"
        f"Lanes and query variants:\n{lanes_text}\n\n"
        f"India-only: only India / Remote-India roles matter. Candidate search focus: "
        f"{profile_summary.get('target_lanes')}, ~{profile_summary.get('experience_years')} years, "
        f"locations {profile_summary.get('preferred_locations')}.{fb}"
    )


@dataclass
class LlmCompanySearchWorker:
    config: PilotConfig
    profile_summary: dict = field(default_factory=dict)
    mode: str = "deterministic"          # "llm" | "deterministic"
    model: str = "claude-sonnet-5"
    base_directory: Optional[str] = None
    session_timeout_s: float = 180.0
    max_turns: int = 40
    http_client_factory: Optional[object] = None   # test seam: () -> ReadOnlyHttpClient-like

    def search_company(self, task: CompanyTask, usage: UsageMeter) -> CompanySearchResult:
        toolbox = CompanySearchToolbox(
            company=task.company, config=self.config, task_id=task.task_id,
            india_only=task.india_only,
            client=(self.http_client_factory() if self.http_client_factory else None),
        )
        started = time.time()
        model_used = "deterministic"
        if self.mode == "llm":
            try:
                model_used = self._run_llm(task, toolbox, usage)
            except Exception as exc:  # noqa: BLE001 - live SDK failure => truthful fallback
                toolbox.limitations.append(f"llm session failed, deterministic fallback: {type(exc).__name__}: {exc}")
                self._run_deterministic(task, toolbox)
                model_used = f"deterministic_fallback(after {self.model})"
        else:
            self._run_deterministic(task, toolbox)

        result = toolbox.submitted or toolbox.build_result()
        result.model = model_used
        result.task_id = task.task_id
        result.tool_calls = toolbox.tool_calls
        result.pages_or_interactions = toolbox.pages_or_interactions
        result.queries_attempted = list(dict.fromkeys(toolbox.queries_attempted))
        result.escalated = task.escalate
        result.jobs = list(toolbox.details)
        result.evidence_urls = list(dict.fromkeys(toolbox.evidence_urls))
        result.lanes = dict(toolbox.lanes)
        result.limitations = list(dict.fromkeys(toolbox.limitations))
        # honest status recompute from what the tools actually produced
        result.status = toolbox._terminal_status(result.status)
        for _ in range(toolbox.tool_calls):
            usage.record_tool_call(model_used)
        return result

    # -- deterministic driver ------------------------------------------------
    def _run_deterministic(self, task: CompanyTask, tb: CompanySearchToolbox) -> None:
        tb.resolve_official_company_site(task.company, [task.domain_hint] if task.domain_hint else None)
        tb.discover_official_careers_entry(domain=task.domain_hint, entry_hint=task.entry_hint)
        rounds = max(1, self.config.max_search_rounds_per_company)
        if tb.discovery and tb.discovery.ats is not None:
            for lane in self.config.primary_lanes:
                for query in self.config.query_for(lane)[:rounds]:
                    tb.search_official_career_site(
                        query, lane=lane, page_budget=self.config.max_pages_or_load_more_per_query,
                    )
            for card in list(tb.cards)[: self.config.max_job_details_per_company]:
                tb.open_official_job_detail(card.url, lane_hint=card.lane_hint)
        elif tb.discovery and tb.discovery.route in ("GENERIC_BROWSER", "GENERIC_HTTP") and not task.feedback:
            # one bounded browser attempt for an SPA/HTML careers shell
            try:
                tb.browser_interact_career_search(tb.discovery.career_entry_url, "submit_search",
                                                  value="Java Developer India")
            except Exception as exc:  # noqa: BLE001
                tb.limitations.append(f"browser attempt failed: {type(exc).__name__}")
        tb.submit_company_search_result()

    # -- live SDK tool loop --------------------------------------------------
    def _run_llm(self, task: CompanyTask, tb: CompanySearchToolbox, usage: UsageMeter) -> str:
        model = self.model
        prompt = build_company_prompt(task, self.config, self.profile_summary)
        tools = build_sdk_tools(tb)
        from atlas.pilot.tools import TOOL_NAMES

        def _permit(request, _context=None):
            # Least privilege: approve ONLY the six pre-validated Atlas tools; deny anything
            # else (builtin shell/edit/git/network are already excluded via available_tools=[]).
            from copilot.generated.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject

            name = getattr(request, "tool_name", "") or getattr(
                getattr(request, "request", None), "tool_name", ""
            )
            if name in TOOL_NAMES:
                return PermissionDecisionApproveOnce()
            return PermissionDecisionReject(feedback="only the six constrained Atlas tools are permitted")

        model_used = {"model": model}

        def _on_event(event) -> None:
            data = getattr(event, "data", None)
            if data is None:
                return
            cname = type(data).__name__
            if cname == "AssistantUsageData":
                usage.record_usage_event(data, model_hint=model)
                if getattr(data, "model", None):
                    model_used["model"] = str(data.model)
            elif cname == "SessionUsageInfoData":
                usage.record_context_info(data)

        async def _run() -> str:
            from copilot import CopilotClient

            done = asyncio.Event()

            def _on_event_wrap(event) -> None:
                _on_event(event)
                if type(getattr(event, "data", None)).__name__ == "SessionIdleData":
                    done.set()

            async with CopilotClient(base_directory=self.base_directory) as client:
                session = await client.create_session(
                    model=model,
                    tools=tools,
                    available_tools=[],  # NO built-in tools: only our six
                    on_permission_request=_permit,
                    on_event=_on_event_wrap,
                    streaming=False,
                )
                async with session:
                    await session.send(prompt)
                    await done.wait()
            return model_used["model"]

        return asyncio.run(asyncio.wait_for(_run(), timeout=self.session_timeout_s))
