"""V4 live agentic company-search worker (prompt s.6, s.17).

One worker searches ONE company. In *live* mode it drives a real Copilot SDK session
(default ``claude-sonnet-5``) granted ONLY the twenty constrained V4 tools
(``available_tools=[]`` denies every built-in) with a default-deny permission handler,
capturing usage into the :class:`~atlas.pilot.usage.UsageMeter`. In *deterministic* mode
(tests / no SDK) the SAME toolbox is driven by Python through the identical surface, so the
pipeline is fully exercisable offline. A company is never "answered from memory": a result
with no tool calls cannot be submitted.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from atlas.pilot.agentic_tools import AgenticCompanyToolbox, V4_TOOL_NAMES, build_v4_sdk_tools
from atlas.pilot.config import PilotConfig
from atlas.pilot.models import CompanySearchResult
from atlas.pilot.status_v4 import CompanySearchStatus, is_genuinely_searched, is_internal_retryable
from atlas.pilot.usage import UsageMeter

__all__ = ["AgenticCompanySearchWorker", "AgenticCompanyTask", "build_agentic_prompt"]

_SENIOR_TITLE = ("senior", "sr.", "sr ", "lead", "principal", "staff", "architect", "advisor",
                 "manager", "director", "head ", "iii", "iv", " ii", "specialist iv")
_JUNIOR_TITLE = ("associate", "junior", "jr", "graduate", "trainee", "entry", "campus", "fresher",
                 "developer", "engineer", "analyst", " i ", "professional i")
_INDIA_TOK = ("india", "bengaluru", "bangalore", "hyderabad", "pune", "chennai", "mumbai", "noida",
              "gurugram", "gurgaon", "delhi", "remote")


def _prioritize_cards(cards: list) -> list:
    """Order job cards so India + junior-looking titles come first, senior last —
    so a small detail-open budget reaches candidate-fit early-career roles."""
    def score(c: dict) -> tuple:
        blob = (str(c.get("title", "")) + " " + str(c.get("location", "")) + " " + str(c.get("url", ""))).lower()
        india = any(t in blob for t in _INDIA_TOK)
        senior = any(t in blob for t in _SENIOR_TITLE)
        junior = any(t in blob for t in _JUNIOR_TITLE)
        return (1 if india else 0, 1 if junior and not senior else 0, 0 if senior else 1)
    return sorted(cards, key=score, reverse=True)


@dataclass
class AgenticCompanyTask:
    company: str
    task_id: str
    domain_hint: str = ""
    entry_hint: str = ""
    india_only: bool = True
    feedback: str = ""
    escalate: bool = False


def build_agentic_prompt(task: AgenticCompanyTask, config: PilotConfig, profile_summary: dict) -> str:
    lanes = "\n".join(f"  - {l}: {', '.join(config.query_for(l)[:3])}" for l in config.primary_lanes)
    locs = ", ".join(config.location_variants) or "India, Bengaluru, Hyderabad, Pune, Chennai, Mumbai, Remote India"
    fb = f"\nRETRY FEEDBACK (address exactly): {task.feedback}\n" if task.feedback else ""
    return (
        f"You are Atlas's OFFICIAL-career search agent for ONE company: {task.company}.\n"
        f"Search ONLY the company's OFFICIAL career site / ATS through the provided tools. Use tools for "
        f"every fact; never answer from memory. STRICTLY read-only: never apply, log in, enter credentials, "
        f"or bypass any challenge. A visible 'Sign in' link is NOT a login wall.\n\n"
        f"BE EFFICIENT AND CHEAP. Prefer the fast paths; use the browser ONLY when they cannot search:\n"
        f"1. resolve_official_company_site (hint: {task.domain_hint or 'unknown'}).\n"
        f"2. discover_official_careers_entry to detect the ATS/route.\n"
        f"3. FAST PATH (preferred): if a public ATS/API is resolved (Workday / Greenhouse / Lever / Ashby), "
        f"call search_official_career_site once per lane (it is cheap HTTP, not a browser) and "
        f"open_official_job_detail on the most plausible India roles. This alone can cover all five lanes.\n"
        f"4. INDEX PATH: if there is no public ATS, use web_search_leads('<lane query> site:{task.domain_hint or 'the official domain'}') "
        f"then web_fetch_official on a TRUSTED official/ATS result to read the indexed detail (cheap HTTP).\n"
        f"5. BROWSER PATH (last resort, only if 3 and 4 cannot search): browser_start(the official career URL). "
        f"For a site that filters via the URL, browser_goto_search(the site's own '?q=<role>&location=India' URL) "
        f"is the MOST reliable — it avoids fragile button clicks. Otherwise, for EACH lane call "
        f"browser_search_lane(lane, query, 'India') (it fills the search box and presses Enter, with a "
        f"short-timeout Search-button fallback); browser_observe to read handles; browser_open_job_detail on "
        f"plausible India roles; browser_back between details. A visible 'Sign in' link is not a wall; keep "
        f"going. If a page click times out, do NOT give up on the company — try browser_goto_search or the next "
        f"lane. Only stop when the browser is truly refused (a hard navigation error) or all five lanes are done.\n"
        f"6. Cover ALL FIVE lanes with 1-2 India-scoped query variants each ({locs}). Keep it tight: aim for "
        f"under ~30 tool calls. As soon as the five lanes are covered and a few India details inspected, "
        f"call submit_company_search_result. ALWAYS submit, even for a truthful blocker "
        f"(ACCESS_LIMITED_EXTERNAL / AUTH_REQUIRED_CONFIRMED / OFFICIAL_SOURCE_UNRESOLVED). Do not loop.\n\n"
        f"Lanes and starter queries:\n{lanes}\n\n"
        f"India-only: only India / Remote-India roles matter. Candidate focus: "
        f"{profile_summary.get('target_lanes')}, ~{profile_summary.get('experience_years')} years, "
        f"locations {profile_summary.get('preferred_locations')}.{fb}"
    )


@dataclass
class AgenticCompanySearchWorker:
    config: PilotConfig
    profile_summary: dict = field(default_factory=dict)
    mode: str = "deterministic"       # "llm" | "deterministic"
    model: str = "claude-sonnet-5"
    base_directory: Optional[str] = None
    session_timeout_s: float = 300.0
    max_turns: int = 60
    headless: bool = True
    browser_factory: Optional[Callable] = None

    def _toolbox(self, task: AgenticCompanyTask) -> AgenticCompanyToolbox:
        return AgenticCompanyToolbox(
            company=task.company, config=self.config, task_id=task.task_id,
            india_only=task.india_only, model=self.model, headless=self.headless,
            browser_factory=self.browser_factory,
        )

    def search_company(self, task: AgenticCompanyTask, usage: UsageMeter) -> CompanySearchResult:
        tb = self._toolbox(task)
        model_used = "deterministic"
        try:
            if self.mode == "llm":
                model_used = self._run_llm(task, tb, usage)
                # Deterministic lane-completion: the LLM directs the search, but if
                # it submitted before covering all five lanes (and the site is not
                # blocked), Python finishes the checklist on the STILL-OPEN browser
                # using the same read-only tools. Zero extra LLM cost; observed
                # searches only (prompt s.6: the governor/validators decide
                # completeness, not the model).
                self._complete_lanes_deterministically(tb)
            else:
                self._run_deterministic(task, tb)
        except Exception as exc:  # noqa: BLE001 - live SDK failure => truthful fallback
            tb.base.limitations.append(f"agentic session failed: {type(exc).__name__}: {exc}")
            model_used = f"{self.model}(session-error)"
        finally:
            tb.cleanup()

        result = tb.submitted or tb.build_result()
        result.model = model_used
        result.task_id = task.task_id
        result.escalated = task.escalate
        result.tool_calls = tb.tool_calls
        for _ in range(tb.tool_calls):
            usage.record_tool_call(model_used)
        return result

    # -- deterministic lane completion on the open browser (LLM mode) --------
    def _complete_lanes_deterministically(self, tb: AgenticCompanyToolbox) -> None:
        primary = self.config.primary_lanes
        remaining = [l for l in primary
                     if not (tb.base.lanes.get(l) and
                             (tb.base.lanes[l].attempted or tb.base.lanes[l].board_snapshot_evaluated))]
        if not remaining:
            return
        # Never override a truthful block; only finish when the browser is usable.
        if tb._browser_access_limited or tb._auth_confirmed:
            return
        d = tb.base.discovery
        # Fast path already covered by the LLM's ATS searches; only the browser
        # path needs deterministic completion. Require a started browser.
        if not tb._browser_started or tb.actor is None:
            return
        entry = (d.career_entry_url if d else "") or ""
        details_budget = self.config.max_job_details_per_company
        for lane in remaining:
            # re-anchor on the careers entry so a search input is present
            if entry:
                nav = tb.browser_goto_search(entry) if "?" in entry else tb.browser_start(entry)
                if not nav.get("ok") and tb._browser_access_limited:
                    return
            for q in (self.config.query_for(lane) or ["Java"])[:1]:
                res = tb.browser_search_lane(lane, q, "India")
                if not res.get("observed"):
                    continue
                # Prioritize India cards whose title looks junior/relevant (not
                # senior/lead) so limited detail-opens reach candidate-fit roles.
                cards = _prioritize_cards(res.get("cards") or [])
                for c in cards[:4]:
                    if len(tb.base.details) >= details_budget:
                        break
                    if c.get("handle"):
                        tb.browser_open_job_detail(handle=c["handle"], lane_hint=lane)
                        tb.browser_back()
        tb.submit_company_search_result()

    # -- deterministic driver -----------------------------------------------
    def _run_deterministic(self, task: AgenticCompanyTask, tb: AgenticCompanyToolbox) -> None:
        tb.resolve_official_company_site(task.company, [task.domain_hint] if task.domain_hint else None)
        tb.discover_official_careers_entry(domain=task.domain_hint, entry_hint=task.entry_hint)
        d = tb.base.discovery
        rounds = max(1, self.config.max_search_rounds_per_company)
        if d is not None and d.ats is not None:
            for lane in self.config.primary_lanes:
                for q in self.config.query_for(lane)[:rounds]:
                    tb.search_official_career_site(q, lane=lane,
                                                   page_budget=self.config.max_pages_or_load_more_per_query)
            for card in list(tb.base.cards)[: self.config.max_job_details_per_company]:
                tb.open_official_job_detail(card.url, lane_hint=card.lane_hint)
        elif d is not None and (d.career_entry_url or task.entry_hint):
            entry = d.career_entry_url or task.entry_hint
            start = tb.browser_start(entry)
            if start.get("ok"):
                for lane in self.config.primary_lanes:
                    q = (self.config.query_for(lane) or ["Java"])[0]
                    res = tb.browser_search_lane(lane, q, "India")
                    for c in (res.get("cards") or [])[:2]:
                        tb.browser_open_job_detail(handle=c.get("handle", ""), lane_hint=lane)
                        tb.browser_back()
        tb.submit_company_search_result()

    # -- live SDK loop -------------------------------------------------------
    def _run_llm(self, task: AgenticCompanyTask, tb: AgenticCompanyToolbox, usage: UsageMeter) -> str:
        prompt = build_agentic_prompt(task, self.config, self.profile_summary)
        tools = build_v4_sdk_tools(tb)
        model_used = {"model": self.model}

        def _permit(request, _ctx=None):
            from copilot.generated.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject
            name = getattr(request, "tool_name", "") or getattr(getattr(request, "request", None), "tool_name", "")
            if name in V4_TOOL_NAMES:
                return PermissionDecisionApproveOnce()
            return PermissionDecisionReject(feedback="only the constrained Atlas V4 tools are permitted")

        def _on_event(event) -> None:
            data = getattr(event, "data", None)
            if data is None:
                return
            cname = type(data).__name__
            if cname == "AssistantUsageData":
                usage.record_usage_event(data, model_hint=self.model)
                if getattr(data, "model", None):
                    model_used["model"] = str(data.model)
            elif cname == "SessionUsageInfoData":
                usage.record_context_info(data)

        async def _run() -> str:
            from copilot import CopilotClient
            done = asyncio.Event()

            def _on_event_wrap(event) -> None:
                _on_event(event)
                if type(getattr(event, "data", None)).__name__ in ("SessionIdleData",):
                    done.set()

            async with CopilotClient(base_directory=self.base_directory) as client:
                session = await client.create_session(
                    model=self.model, tools=tools, available_tools=[],
                    on_permission_request=_permit, on_event=_on_event_wrap, streaming=False,
                )
                async with session:
                    await session.send(prompt)
                    await done.wait()
            return model_used["model"]

        return asyncio.run(asyncio.wait_for(_run(), timeout=self.session_timeout_s))
