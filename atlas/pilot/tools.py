"""The six constrained, official-only, read-only tools the LLM company agent may use
(architecture s.4). Each tool is a thin, deterministic Python method that performs the
actual HTTP/browser work through the bounded :class:`ReadOnlyHttpClient` and returns typed,
JSON-serializable evidence. Python — not the model — validates every URL (HTTPS + official/
ATS host), enforces read-only, and bounds pages/details. The model can direct *which*
queries/pages to pursue, but can never widen the safety envelope.

``build_sdk_tools`` wraps these methods as official Copilot SDK ``Tool`` objects (lazy import
so the module never requires the SDK). The same toolbox drives the deterministic fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlsplit

from atlas.pilot.config import PilotConfig
from atlas.pilot.discovery import (
    DiscoveryResult,
    discover_careers_entry,
    fetch_job_detail,
    make_http_client,
    resolve_official_domain,
    search_official_source,
)
from atlas.pilot.models import (
    CompanySearchResult,
    CompanyStatus,
    JobCard,
    JobDetailEvidence,
    JobRejection,
    LaneCoverage,
)
from atlas.sources.http_client import ReadOnlyHttpClient

# ATS hosts that are legitimate employer-controlled public endpoints.
_ATS_HOSTS = (
    "boards-api.greenhouse.io", "boards.greenhouse.io", "api.lever.co", "jobs.lever.co",
    "api.ashbyhq.com", "jobs.ashbyhq.com",
)

_ALLOWED_BROWSER_ACTIONS = frozenset(
    {"fill_search_text", "choose_india_location", "submit_search", "next_page",
     "load_more", "scroll", "open_job_detail"}
)


def _host(url: str) -> str:
    return (urlsplit(url).netloc or "").split("@")[-1].split(":")[0].lower().rstrip(".")


@dataclass
class CompanySearchToolbox:
    """Holds the bounded state for ONE company's search and exposes the six tools."""

    company: str
    config: PilotConfig
    task_id: str = ""
    client: Optional[ReadOnlyHttpClient] = None
    discovery: Optional[DiscoveryResult] = None
    india_only: bool = True
    tool_calls: int = 0
    pages_or_interactions: int = 0
    queries_attempted: list[str] = field(default_factory=list)
    cards: list[JobCard] = field(default_factory=list)
    details: list[JobDetailEvidence] = field(default_factory=list)
    lanes: dict[str, LaneCoverage] = field(default_factory=dict)
    evidence_urls: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    submitted: Optional[CompanySearchResult] = None
    _details_budget: int = 30

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = make_http_client(budget=200)
        self._details_budget = self.config.max_job_details_per_company
        for lane in self.config.primary_lanes:
            self.lanes.setdefault(lane, LaneCoverage(lane=lane))

    # -- URL trust -----------------------------------------------------------
    def _official_hosts(self) -> tuple[str, ...]:
        hosts = list(_ATS_HOSTS)
        dom = (self.discovery.official_domain if self.discovery else "") or ""
        if dom:
            hosts.append(dom)
        # any myworkdayjobs subdomain is an employer-controlled ATS host
        return tuple(hosts)

    def _is_official_url(self, url: str) -> bool:
        if not url or not url.lower().startswith("https://"):
            return False
        host = _host(url)
        if host.endswith(".myworkdayjobs.com") or host == "myworkdayjobs.com":
            return True
        for h in self._official_hosts():
            if host == h or host.endswith("." + h) or h.endswith("." + host):
                return True
        return False

    # -- tool 1 --------------------------------------------------------------
    def resolve_official_company_site(self, company: str = "", domain_hints: Optional[list[str]] = None) -> dict:
        self.tool_calls += 1
        company = company or self.company
        hint = {company.lower(): domain_hints[0]} if domain_hints else None
        domain, prov = resolve_official_domain(company, hint)
        return {
            "company": company,
            "official_domain": domain,
            "provenance": prov,
            "candidates": [domain] if domain else [],
            "https_required": True,
        }

    # -- tool 2 --------------------------------------------------------------
    def discover_official_careers_entry(self, domain: str = "", entry_hint: str = "") -> dict:
        self.tool_calls += 1
        self.discovery = discover_careers_entry(
            self.company, client=self.client, domain_hint=domain or None, entry_hint=entry_hint or None,
        )
        for u in self.discovery.evidence_urls:
            if u not in self.evidence_urls:
                self.evidence_urls.append(u)
        for lim in self.discovery.limitations:
            if lim not in self.limitations:
                self.limitations.append(lim)
        return {
            "official_domain": self.discovery.official_domain,
            "career_entry_url": self.discovery.career_entry_url,
            "route": self.discovery.route,
            "source_family": self.discovery.source_family,
            "ats_resolved": self.discovery.ats is not None,
            "status": self.discovery.status,
            "redirect_chain": self.discovery.redirect_chain,
            "limitations": self.discovery.limitations,
        }

    # -- tool 3 --------------------------------------------------------------
    def search_official_career_site(
        self, query: str, lane: str = "", india_locations: Optional[list[str]] = None, page_budget: int = 3,
    ) -> dict:
        self.tool_calls += 1
        if self.discovery is None or self.discovery.ats is None:
            return {"error": "no resolved public ATS source; call discover_official_careers_entry first",
                    "cards": [], "status": (self.discovery.status if self.discovery else
                                            CompanyStatus.OFFICIAL_SOURCE_UNRESOLVED.value)}
        page_budget = min(int(page_budget or 1), self.config.max_pages_or_load_more_per_query)
        provider = self.discovery.ats[0]
        # A list board (greenhouse/lever/ashby) is fetched whole and evaluated locally, so we
        # do NOT pre-filter India at the source: the geography gate routes India -> main and
        # foreign -> foreign_leads. A search API (Workday) is India-faceted at the source.
        list_board = provider in ("greenhouse", "lever", "ashby")
        effective_india_only = self.india_only and not list_board
        cards, limitations = search_official_source(
            self.discovery, query, client=self.client, india_only=effective_india_only,
            page_budget=page_budget,
        )
        self.queries_attempted.append(query)
        self.pages_or_interactions += max(1, page_budget)
        if lane and lane in self.lanes:
            cov = self.lanes[lane]
            cov.attempted = True
            cov.queries.append(query)
            cov.pages += page_budget
            cov.candidates += len(cards)
            # Greenhouse/Lever/Ashby return the WHOLE board in one fetch, so a single
            # successful fetch is a complete list-board snapshot evaluated for this lane.
            if self.discovery.ats and self.discovery.ats[0] in ("greenhouse", "lever", "ashby"):
                cov.board_snapshot_evaluated = True
        # de-dup cards by url
        seen = {c.url for c in self.cards}
        for c in cards:
            if c.url and c.url not in seen:
                self.cards.append(c)
                seen.add(c.url)
        for lim in limitations:
            if lim not in self.limitations:
                self.limitations.append(lim)
        return {
            "query": query,
            "lane": lane,
            "cards": [c.to_dict() for c in cards[:40]],
            "count": len(cards),
            "limitations": limitations,
            "source_family": self.discovery.source_family,
        }

    # -- tool 4 --------------------------------------------------------------
    def open_official_job_detail(self, job_url: str, lane_hint: str = "") -> dict:
        self.tool_calls += 1
        if not self._is_official_url(job_url):
            return {"error": f"refused non-official/non-HTTPS URL: {job_url}"}
        if len(self.details) >= self._details_budget:
            return {"error": "job-detail budget exhausted for this company"}
        card = next((c for c in self.cards if c.url == job_url),
                    JobCard(title="", url=job_url, lane_hint=lane_hint))
        detail = fetch_job_detail(card, self.company, discovery=self.discovery, client=self.client)
        if detail is None:
            return {"error": "could not fetch official job detail (blocked/unsupported)"}
        self.details.append(detail)
        if job_url not in self.evidence_urls:
            self.evidence_urls.append(job_url)
        self.pages_or_interactions += 1
        return {"detail": detail.to_dict()}

    # -- tool 5 --------------------------------------------------------------
    def browser_interact_career_search(self, career_url: str, action: str, value: str = "") -> dict:
        self.tool_calls += 1
        if action not in _ALLOWED_BROWSER_ACTIONS:
            return {"error": f"action {action!r} not allowed; permitted: {sorted(_ALLOWED_BROWSER_ACTIONS)}"}
        if not self._is_official_url(career_url) and not (self.discovery and
                                                          _host(career_url) == _host(self.discovery.career_entry_url or "")):
            return {"error": f"refused non-official career URL: {career_url}"}
        # Bounded browser interaction is best-effort and read-only. Enterprise SPAs behind
        # anti-bot frequently block automation; that is reported truthfully, never bypassed.
        from atlas.pilot.browser import bounded_browser_action

        result = bounded_browser_action(career_url, action, value=value, india_only=self.india_only)
        self.pages_or_interactions += 1
        cards = [JobCard(**c) for c in result.get("cards", [])]
        seen = {c.url for c in self.cards}
        for c in cards:
            if c.url and c.url not in seen:
                self.cards.append(c)
                seen.add(c.url)
        if result.get("limitation"):
            self.limitations.append(result["limitation"])
        return result

    # -- tool 6 --------------------------------------------------------------
    def submit_company_search_result(self, result: Optional[dict] = None, status: str = "") -> dict:
        self.tool_calls += 1
        csr = self.build_result(status_hint=status)
        self.submitted = csr
        return {"accepted": True, "status": csr.status,
                "lanes_complete": csr.lane_checklist_complete(self.config.primary_lanes),
                "jobs": len(csr.jobs)}

    # -- assembly ------------------------------------------------------------
    def build_result(self, *, status_hint: str = "", model: str = "") -> CompanySearchResult:
        d = self.discovery
        status = self._terminal_status(status_hint)
        return CompanySearchResult(
            company=self.company,
            official_domain=(d.official_domain if d else ""),
            career_entry_url=(d.career_entry_url if d else ""),
            route=(d.route if d else "UNRESOLVED"),
            source_family=(d.source_family if d else ""),
            status=status,
            lanes=dict(self.lanes),
            jobs=list(self.details),
            rejections=[],
            limitations=list(dict.fromkeys(self.limitations)),
            evidence_urls=list(dict.fromkeys(self.evidence_urls)),
            model=model,
            task_id=self.task_id,
            tool_calls=self.tool_calls,
            queries_attempted=list(self.queries_attempted),
            pages_or_interactions=self.pages_or_interactions,
        )

    def _terminal_status(self, status_hint: str) -> str:
        d = self.discovery
        if d is None:
            return CompanyStatus.OFFICIAL_SOURCE_UNRESOLVED.value
        if d.ats is None:
            # discovery resolved a truthful non-ATS status (unsupported/auth/access/unresolved)
            return d.status
        # a public ATS source was searched
        if self.details:
            return CompanyStatus.COMPLETE.value
        if self.queries_attempted:
            return CompanyStatus.COMPLETE_NO_MATCHES.value
        return CompanyStatus.COMPLETE_NO_MATCHES.value


__all__ = ["CompanySearchToolbox", "build_sdk_tools", "_ALLOWED_BROWSER_ACTIONS"]


def build_sdk_tools(toolbox: CompanySearchToolbox) -> list[Any]:
    """Wrap the toolbox methods as official Copilot SDK Tool objects (lazy SDK import)."""
    from copilot import define_tool
    from pydantic import BaseModel, Field

    class ResolveParams(BaseModel):
        company: str = Field(default="", description="Company name (defaults to the assigned company)")
        domain_hints: list[str] = Field(default_factory=list, description="Known verified official domains")

    class DiscoverParams(BaseModel):
        domain: str = Field(default="", description="Verified official company domain")
        entry_hint: str = Field(default="", description="Optional known official careers URL")

    class SearchParams(BaseModel):
        query: str = Field(description="Short role query, e.g. 'Java Developer'")
        lane: str = Field(default="", description="Target lane key this query serves")
        india_locations: list[str] = Field(default_factory=list, description="India location filters")
        page_budget: int = Field(default=3, description="Max pages / load-more for this query")

    class DetailParams(BaseModel):
        job_url: str = Field(description="Validated official job detail URL (HTTPS, official host)")
        lane_hint: str = Field(default="", description="Lane this job appears to serve")

    class BrowserParams(BaseModel):
        career_url: str = Field(description="Validated official career search URL")
        action: str = Field(description="One of: fill_search_text, choose_india_location, submit_search, "
                                        "next_page, load_more, scroll, open_job_detail")
        value: str = Field(default="", description="Text/location for fill/choose/open actions")

    class SubmitParams(BaseModel):
        status: str = Field(default="", description="Optional terminal status hint")

    def _wrap(fn):
        def handler(params, _inv):
            kwargs = params.model_dump() if hasattr(params, "model_dump") else dict(params)
            import json as _json
            try:
                return _json.dumps(fn(**kwargs), ensure_ascii=False)[:12000]
            except Exception as exc:  # noqa: BLE001 - surfaced to the model as a tool error
                return _json.dumps({"error": f"{type(exc).__name__}: {exc}"})
        return handler

    return [
        define_tool("resolve_official_company_site", description="Resolve a company's official career domain(s) with provenance. HTTPS official only.",
                    handler=_wrap(toolbox.resolve_official_company_site), params_type=ResolveParams, skip_permission=True),
        define_tool("discover_official_careers_entry", description="Fetch the official careers entry read-only and detect the ATS/route.",
                    handler=_wrap(toolbox.discover_official_careers_entry), params_type=DiscoverParams, skip_permission=True),
        define_tool("search_official_career_site", description="Search the resolved official/ATS source for India roles matching a query.",
                    handler=_wrap(toolbox.search_official_career_site), params_type=SearchParams, skip_permission=True),
        define_tool("open_official_job_detail", description="Open a validated official job detail URL and extract bounded evidence.",
                    handler=_wrap(toolbox.open_official_job_detail), params_type=DetailParams, skip_permission=True),
        define_tool("browser_interact_career_search", description="Perform ONE bounded, read-only browser action on the official career search page.",
                    handler=_wrap(toolbox.browser_interact_career_search), params_type=BrowserParams, skip_permission=True),
        define_tool("submit_company_search_result", description="Submit the typed company search result with the lane checklist. Call this last.",
                    handler=_wrap(toolbox.submit_company_search_result), params_type=SubmitParams, is_terminal=True, skip_permission=True),
    ]


#: The exact set of tool names the company agent may ever call.
TOOL_NAMES = frozenset({
    "resolve_official_company_site", "discover_official_careers_entry", "search_official_career_site",
    "open_official_job_detail", "browser_interact_career_search", "submit_company_search_result",
})
