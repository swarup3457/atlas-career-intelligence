"""Unified V4 agentic tool surface for the live company-search agent (prompt s.4, s.5, s.6).

Composes, for ONE company, the constrained tools the live LLM agent may call:

* HTTP/ATS fast paths (reused from :class:`atlas.pilot.tools.CompanySearchToolbox`):
  resolve official domain, discover careers entry, ATS search, ATS job detail;
* the twelve stateful, read-only browser tools bound to ONE persistent
  :class:`atlas.pilot.browser_actor.CompanyBrowserActor` (created lazily on
  ``browser_start`` and retained for the whole company);
* the two constrained web-discovery tools
  (:class:`atlas.pilot.discovery_tools.WebDiscoveryTools`), trust-validated in Python;
* ``submit_company_search_result``, which builds the typed
  :class:`~atlas.pilot.models.CompanySearchResult` and assigns a V4 status.

Every browser action references a stable HANDLE from the latest observation; every
opened detail is captured as typed :class:`~atlas.pilot.models.JobDetailEvidence` so the
deterministic gates can adjudicate it. Python — not the model — validates URLs, enforces
read-only, and decides the truthful terminal/retryable V4 status.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from atlas.pilot.browser_actor import BrowserActorError, CompanyBrowserActor
from atlas.pilot.config import PilotConfig
from atlas.pilot.discovery_tools import WebDiscoveryTools, is_trusted_official_url
from atlas.pilot.models import CompanySearchResult, JobDetailEvidence
from atlas.pilot.normalize import normalize_source_text, split_sections
from atlas.pilot.status_v4 import (
    AccessSignals,
    CompanySearchStatus,
    classify_blocker,
    looks_like_login_wall,
)
from atlas.pilot.tools import CompanySearchToolbox

__all__ = ["AgenticCompanyToolbox", "build_v4_sdk_tools", "V4_TOOL_NAMES", "looks_like_job_title"]

_EXPERIENCE_HINT = ("experience", "years", "yrs")

# Publicly-known SECONDARY official career hosts for the fixed cohort. An employer
# often serves its careers on a different registrable domain than its main site
# (e.g. jpmorganchase.com vs careers.jpmorgan.com). These are public official
# domains, added ONLY to widen the browser's trusted-host set — never used to seed
# job answers. Keyed by lowercased company name.
SECONDARY_OFFICIAL_HOSTS: dict = {
    "jpmorgan chase": ("jpmorgan.com", "careers.jpmorgan.com", "jpmc.com"),
    "jpmorgan": ("jpmorgan.com", "careers.jpmorgan.com"),
    "tcs": ("ibegin.tcs.com", "tcs.com"),
    "cognizant": ("careers.cognizant.com",),
    "infosys": ("career.infosys.com", "digitalcareers.infosys.com"),
    "oracle": ("careers.oracle.com", "oracle.com"),
    "sap": ("jobs.sap.com", "sap.com"),
    "ibm": ("careers.ibm.com", "ibm.com"),
    "adp": ("jobs.adp.com", "workforcenow.adp.com", "adp.com"),
    "accenture": ("accenture.com",),
    "fiserv": ("careers.fiserv.com", "fiserv.com"),
}

# A captured "job detail" is only a real posting when its title reads like a role.
# The live browser/web path can otherwise capture a page banner (e.g. ADP's
# "YOU ARE ONE STEP CLOSER TO FINDING YOUR NEXT JOB") as a job — such non-jobs
# must never become candidate evidence.
_ROLE_TITLE_TOKENS = (
    "developer", "engineer", "architect", "analyst", "consultant", "programmer", "specialist",
    "administrator", "scientist", "designer", "tester", "sde", "lead", "manager", "software",
    "full stack", "fullstack", "frontend", "backend", "front end", "back end", "qa", "devops",
    "sre", "technologist", "associate", "professional", "intern", "trainee", "principal", "staff",
    "member of technical staff", "development", "dev ", " dev", "engineering", "coder", "technician",
)
_NON_JOB_PHRASES = (
    "one step closer", "find your", "next job", "job search", "search jobs", "welcome",
    "sign in", "log in", "your next", "explore", "life at", "why ", "benefits", "careers",
    "cookie", "privacy", "results for", "no results", "loading", "apply now",
)


def looks_like_job_title(title: object) -> bool:
    t = (str(title or "")).strip()
    low = t.lower()
    if not t or len(t) > 120 or len(t) < 3:
        return False
    if any(p in low for p in _NON_JOB_PHRASES):
        return False
    return any(tok in low for tok in _ROLE_TITLE_TOKENS)


def _host(url: str) -> str:
    return (urlsplit(url or "").netloc or "").split("@")[-1].split(":")[0].lower().rstrip(".")


@dataclass
class AgenticCompanyToolbox:
    """Holds the bounded state + tools for ONE company's live agentic search."""

    company: str
    config: PilotConfig
    task_id: str = ""
    india_only: bool = True
    model: str = ""
    headless: bool = True
    base: Optional[CompanySearchToolbox] = None
    browser_factory: Optional[Callable[..., CompanyBrowserActor]] = None
    web: Optional[WebDiscoveryTools] = None
    actor: Optional[CompanyBrowserActor] = None
    submitted: Optional[CompanySearchResult] = None
    # blocker / error signals (Python-decided terminality)
    _auth_confirmed: bool = False          # BROWSER-confirmed login wall only
    _http_access_limited: bool = False     # a plain-HTTP (non-JS) discovery 403 — a WEAK signal
    _browser_access_limited: bool = False  # the BROWSER itself was refused (hard nav block)
    _browser_error: bool = False           # retryable browser/tool hiccup (e.g. click timeout)
    _async_error: bool = False
    _browser_started: bool = False
    _browser_searched: bool = False        # the browser produced at least one observed result set
    _web_lead_found: bool = False
    extra_trusted_hosts: tuple = ()
    action_budget: int = 60

    def __post_init__(self) -> None:
        if self.base is None:
            self.base = CompanySearchToolbox(
                company=self.company, config=self.config, task_id=self.task_id,
                india_only=self.india_only,
            )
        if self.web is None:
            self.web = WebDiscoveryTools(official_domain="")

    # -- convenience accessors ----------------------------------------------
    @property
    def tool_calls(self) -> int:
        return self.base.tool_calls + (self.actor.action_count if self.actor else 0)

    def _sync_web_domain(self) -> None:
        dom = (self.base.discovery.official_domain if self.base.discovery else "") or ""
        if dom and self.web.official_domain != dom:
            self.web.official_domain = dom

    # -- HTTP/ATS fast paths (reuse) ----------------------------------------
    def resolve_official_company_site(self, company: str = "", domain_hints: Optional[list[str]] = None) -> dict:
        out = self.base.resolve_official_company_site(company, domain_hints)
        if out.get("official_domain"):
            self.web.official_domain = out["official_domain"]
        return out

    def discover_official_careers_entry(self, domain: str = "", entry_hint: str = "") -> dict:
        out = self.base.discover_official_careers_entry(domain, entry_hint)
        self._sync_web_domain()
        d = self.base.discovery
        # A plain-HTTP (non-JS, Atlas-UA) discovery 403/access status is a WEAK
        # signal: enterprise WAFs routinely refuse a non-browser client while a
        # real browser renders fine (prompt s.3.4 — 403 is ACCESS_LIMITED unless
        # the BROWSER confirms a wall). Record it, but the stateful browser is the
        # real arbiter — never let it terminate the company on its own.
        if d is not None and d.status in CompanySearchStatus_legacy_access():
            self._http_access_limited = True
        return out

    def search_official_career_site(self, query: str, lane: str = "",
                                    india_locations: Optional[list[str]] = None, page_budget: int = 3) -> dict:
        return self.base.search_official_career_site(query, lane=lane, india_locations=india_locations,
                                                     page_budget=page_budget)

    def open_official_job_detail(self, job_url: str, lane_hint: str = "") -> dict:
        return self.base.open_official_job_detail(job_url, lane_hint=lane_hint)

    # -- web discovery (trust-validated) ------------------------------------
    def web_search_leads(self, query: str, site_scope: bool = True) -> dict:
        self._sync_web_domain()
        self.base.tool_calls += 1
        out = self.web.web_search_leads(query, site_scope=site_scope)
        if out.get("trusted_count", 0) > 0:
            self._web_lead_found = True
        return out

    def web_fetch_official(self, url: str) -> dict:
        self._sync_web_domain()
        self.base.tool_calls += 1
        out = self.web.web_fetch_official(url)
        for u in out.get("official_job_links", []) or []:
            if u.get("url") and u["url"] not in self.base.evidence_urls:
                self.base.evidence_urls.append(u["url"])
        return out

    # -- stateful browser (12 tools) ----------------------------------------
    def _trusted_hosts(self) -> tuple:
        hosts = list(self.extra_trusted_hosts)
        d = self.base.discovery
        if d is not None and d.official_domain:
            hosts.append(d.official_domain)
        # publicly-known secondary official career hosts for the fixed cohort
        # (an employer often serves careers on a different registrable domain,
        # e.g. jpmorganchase.com vs careers.jpmorgan.com). Public info, not seeds.
        hosts.extend(SECONDARY_OFFICIAL_HOSTS.get(self.company.lower(), ()))
        seen, out = set(), []
        for h in hosts:
            h = (h or "").lower().lstrip(".")
            if h and h not in seen:
                seen.add(h)
                out.append(h)
        return tuple(out)

    def _ensure_actor(self) -> CompanyBrowserActor:
        if self.actor is None:
            factory = self.browser_factory
            trusted = self._trusted_hosts()
            if factory is not None:
                self.actor = factory(actor_id=f"{self.task_id or self.company}", company=self.company,
                                     task_id=self.task_id, trusted_hosts=trusted)
            else:
                self.actor = CompanyBrowserActor(
                    actor_id=f"{self.task_id or self.company}", company=self.company, task_id=self.task_id,
                    headless=self.headless, trusted_hosts=trusted, action_budget=self.action_budget,
                )
        return self.actor

    def _guard(self, fn, *a, is_navigation: bool = False, **kw) -> dict:
        from atlas.pilot.browser_actor import is_hard_navigation_block
        try:
            return {"ok": True, **(fn(*a, **kw) or {})}
        except BrowserActorError as exc:
            msg = str(exc)
            low = msg.lower()
            if "asyncio" in low or "event loop" in low:
                self._async_error = True
                self.base.limitations.append(f"async runtime error (retryable): {msg[:160]}")
            elif is_navigation and is_hard_navigation_block(msg):
                # the BROWSER itself was refused by the server -> a real external block
                self._browser_access_limited = True
                self.base.limitations.append(f"browser-confirmed access block: {msg[:160]}")
            else:
                # a soft interaction hiccup (e.g. a click/locator timeout) -> retryable
                self._browser_error = True
                self.base.limitations.append(f"browser tool error (retryable): {msg[:160]}")
            return {"ok": False, "error": msg, "retryable": not self._browser_access_limited}
        except Exception as exc:  # noqa: BLE001
            self._browser_error = True
            self.base.limitations.append(f"browser tool error (retryable): {type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "retryable": True}

    def _note_challenge(self, obs: dict) -> None:
        ch = (obs or {}).get("challenge") or {}
        content = bool(obs.get("job_cards") or obs.get("headings"))
        if content:
            self._browser_searched = True
        if looks_like_login_wall(password_field_present=ch.get("password_field", False),
                                 job_content_visible=content,
                                 signin_link_only=ch.get("signin_link", False)):
            self._auth_confirmed = True

    def browser_start(self, url: str) -> dict:
        self._browser_started = True
        actor = self._ensure_actor()
        out = self._guard(actor.start, url, is_navigation=True)
        if out.get("ok"):
            self._note_challenge(out)
        return out

    def browser_goto_search(self, url: str) -> dict:
        """Navigate the persistent page directly to a trusted search-results URL
        (query/location params) — reliable for SPAs that filter via the URL and
        avoids fragile button clicks."""
        self._browser_started = True
        actor = self._ensure_actor()
        out = self._guard(actor.goto_search, url, is_navigation=True)
        if out.get("ok"):
            self._note_challenge(out)
        return out

    def browser_observe(self) -> dict:
        out = self._guard(self._ensure_actor().observe)
        if out.get("ok"):
            self._note_challenge(out)
        return out

    def browser_fill(self, handle: str, value: str) -> dict:
        return self._guard(self._ensure_actor().fill, handle, value)

    def browser_click(self, handle: str) -> dict:
        return self._guard(self._ensure_actor().click, handle)

    def browser_press(self, key: str, handle: str = "") -> dict:
        return self._guard(self._ensure_actor().press, key, handle)

    def browser_select_option(self, handle: str, value: str) -> dict:
        return self._guard(self._ensure_actor().select_option, handle, value)

    def browser_wait(self, ms: int = 800, state: str = "") -> dict:
        return self._guard(self._ensure_actor().wait, ms=ms, state=state)

    def browser_scroll_or_load_more(self, handle: str = "") -> dict:
        return self._guard(self._ensure_actor().scroll_or_load_more, handle)

    def browser_collect_job_cards(self, lane: str = "") -> dict:
        out = self._guard(self._ensure_actor().collect_job_cards)
        if out.get("ok") and lane and lane in self.base.lanes:
            cov = self.base.lanes[lane]
            cov.attempted = True
            cov.candidates += int(out.get("count", 0))
        return out

    def browser_open_job_detail(self, handle: str = "", url: str = "", lane_hint: str = "") -> dict:
        actor = self._ensure_actor()
        # Capture the card's title/location BEFORE navigating (the handle map is
        # replaced by the detail page's observation once we navigate).
        pre_card = dict(getattr(actor, "_last_cards", {}).get(handle, {})) if handle else {}
        out = self._guard(actor.open_job_detail, handle, url, is_navigation=True)
        if out.get("ok"):
            # If the browser capture is thin (a share widget / pre-render shell on a
            # hard SPA), try a read-only HTTP fetch of the SAME official job URL —
            # some sites expose SSR/JSON-LD the browser render missed. Within rules.
            text = (out.get("detail_text") or "")
            if len(text) < 300:
                job_url = out.get("url") or pre_card.get("url") or url
                if job_url:
                    fetched = self.web_fetch_official(job_url)
                    ft = fetched.get("text") or ""
                    if len(ft) > len(text):
                        out["detail_text"] = ft
                        out["detail_source"] = "web_fetch_official_fallback"
            self._capture_detail(out, card=pre_card, lane_hint=lane_hint)
        return out

    def browser_back(self) -> dict:
        return self._guard(self._ensure_actor().back)

    def browser_close(self) -> dict:
        if self.actor is not None:
            try:
                return {"ok": True, **(self.actor.close() or {})}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "note": "no browser to close"}

    # -- convenience: one lane search on the current browser page -----------
    def browser_search_lane(self, lane: str, query: str, india_location: str = "India") -> dict:
        """Compose a bounded, robust lane search on the live page: fill the first
        search input, set an India location if present, submit (short click with an
        Enter fallback), wait, then observe + collect cards. A single fragile click
        never aborts the lane. The lane is marked ATTEMPTED only when a result set
        was actually OBSERVED (prompt s.9: observed query/filter/results)."""
        actor = self._ensure_actor()
        obs = self._guard(actor.observe)
        if not obs.get("ok"):
            return {"ok": False, "lane": lane, "query": query, "error": obs.get("error"), "observed": False}
        self._note_challenge(obs)
        search = next((i for i in obs.get("inputs", [])
                       if any(k in (i.get("placeholder", "") + i.get("label", "") + i.get("name", "")).lower()
                              for k in ("search", "keyword", "title", "role", "what"))), None)
        if search:
            self._guard(actor.fill, search["handle"], query)
        loc = next((i for i in obs.get("inputs", [])
                    if any(k in (i.get("placeholder", "") + i.get("label", "") + i.get("name", "")).lower()
                           for k in ("location", "where", "city"))), None)
        if loc:
            if loc.get("tag") == "select":
                self._guard(actor.select_option, loc["handle"], india_location)
            else:
                self._guard(actor.fill, loc["handle"], india_location)
        # submit: prefer Enter in the search field (reliable, no navigation wait),
        # then a short-timeout Search-button click as a fallback. Neither aborts.
        submitted = False
        if search:
            r = self._guard(actor.press, "Enter", search["handle"])
            submitted = r.get("ok", False)
        if not submitted:
            btn = next((b for b in obs.get("buttons", []) if "search" in (b.get("text", "").lower())), None)
            if btn:
                self._guard(actor.click, btn["handle"], timeout_ms=5000)
        self._guard(actor.wait, ms=1400)
        cards = self._guard(actor.collect_job_cards)
        observed = cards.get("ok", False)
        if observed:
            self._browser_searched = True
        if lane in self.base.lanes and observed:
            cov = self.base.lanes[lane]
            cov.attempted = True
            cov.queries.append(query)
            cov.pages += 1
            cov.candidates += int(cards.get("count", 0))
        self.base.queries_attempted.append(query)
        self.base.pages_or_interactions += 1
        return {"ok": observed, "lane": lane, "query": query, "observed": observed,
                "cards": cards.get("job_cards", []) if observed else [],
                "count": cards.get("count", 0) if observed else 0}

    def _capture_detail(self, detail_out: dict, *, card: Optional[dict] = None, handle: str = "",
                        lane_hint: str = "") -> None:
        text = normalize_source_text(detail_out.get("detail_text", ""))
        if not text:
            return
        if card is None:
            card = {}
            if self.actor is not None:
                card = getattr(self.actor, "_last_cards", {}).get(handle, {})
        headings = detail_out.get("headings") or []
        # Prefer the card title (captured pre-navigation), else the FIRST heading that
        # reads like a job title (the page header/banner is skipped).
        title = (card.get("title")
                 or next((h for h in headings if looks_like_job_title(h)), "")
                 or detail_out.get("title", ""))
        title = (title or "")[:180]
        # Reject non-job captures (page banners / marketing headers) so they never
        # become candidate evidence, even if the page text mentions India + a domain word.
        if not looks_like_job_title(title):
            return
        # Reject THIN captures: on a hard SPA the browser sometimes grabs nav/footer
        # text (e.g. "Email X LinkedIn") instead of the real JD. Require a plausible
        # amount of text AND at least one job-content marker, so thin/nav-only
        # captures never pollute the evidence set (integrity, not padding).
        low = text.lower()
        if len(text) < 200 or not any(
            m in low for m in ("experience", "responsib", "qualif", "skill", "requirement",
                               "develop", "engineer", "years", "team", "role", "candidate")):
            self.base.limitations.append(f"thin/low-signal job capture skipped: {title[:60]!r}")
            return
        location = card.get("location", "")
        if not location:
            st = split_sections(text)
            # best-effort: an India/location token from the text
            for tok in ("India", "Bengaluru", "Bangalore", "Hyderabad", "Pune", "Chennai", "Mumbai",
                        "Noida", "Gurugram", "Gurgaon", "Delhi", "Remote"):
                if tok.lower() in text.lower():
                    location = tok
                    break
        url = detail_out.get("url", "")
        det = JobDetailEvidence(
            title=title or "(untitled)", company=self.company, location=location,
            description=text[:6000], experience_text=text[:6000], official_url=url,
            source_family="OFFICIAL_CAREERS_BROWSER", evidence_snippets=(text[:400],),
        )
        # de-dup by url
        if not any(j.official_url == url and j.title == det.title for j in self.base.details):
            self.base.details.append(det)
            if url and url not in self.base.evidence_urls:
                self.base.evidence_urls.append(url)

    # -- submit --------------------------------------------------------------
    def _v4_status(self) -> str:
        primary = self.config.primary_lanes
        searched_lanes = [
            l for l in primary
            if self.base.lanes.get(l) and
            (self.base.lanes[l].attempted or self.base.lanes[l].board_snapshot_evaluated)
        ]
        all_lanes = len(searched_lanes) == len(primary)
        details = len(self.base.details)
        d = self.base.discovery
        made_progress = bool(searched_lanes or self._browser_searched)

        # All five lanes genuinely searched -> the company is genuinely searched.
        if all_lanes:
            return (CompanySearchStatus.SEARCHED_COMPLETE_WITH_MATCHES.value if details
                    else CompanySearchStatus.SEARCHED_COMPLETE_NO_MATCHES.value)
        # a public complete board evaluated once satisfies all lanes
        if d is not None and d.ats is not None and d.ats[0] in ("greenhouse", "lever", "ashby"):
            if any(self.base.lanes[l].board_snapshot_evaluated for l in primary):
                return (CompanySearchStatus.SEARCHED_COMPLETE_WITH_MATCHES.value if details
                        else CompanySearchStatus.SEARCHED_COMPLETE_NO_MATCHES.value)

        # BROWSER-confirmed external blocks are truthful terminal outcomes.
        if self._auth_confirmed:
            return CompanySearchStatus.AUTH_REQUIRED_CONFIRMED.value
        if self._browser_access_limited and not made_progress:
            return CompanySearchStatus.ACCESS_LIMITED_EXTERNAL.value

        # Internal / retryable faults (never terminal) — the governor retries.
        if self._async_error and not made_progress:
            return CompanySearchStatus.ASYNC_RUNTIME_ERROR.value
        if self._browser_error and not made_progress:
            return CompanySearchStatus.BROWSER_TOOL_ERROR.value

        # Nothing resolved and nothing searched: if ONLY a plain-HTTP (non-JS) 403
        # was seen and the browser never confirmed a block, that is a weak external
        # signal -> ACCESS_LIMITED_EXTERNAL (truthful, not searched); otherwise the
        # source was never resolved.
        if not made_progress:
            if self._browser_access_limited or (self._http_access_limited and self._browser_started):
                return CompanySearchStatus.ACCESS_LIMITED_EXTERNAL.value
            if d is None or (not d.resolved and not self._browser_started and not self._web_lead_found):
                if self._http_access_limited:
                    return CompanySearchStatus.ACCESS_LIMITED_EXTERNAL.value
                return CompanySearchStatus.OFFICIAL_SOURCE_UNRESOLVED.value
            return CompanySearchStatus.OFFICIAL_SOURCE_UNRESOLVED.value

        # Some lanes searched but not all five -> incomplete (retryable), so the
        # governor retries THIS company to finish the checklist.
        return CompanySearchStatus.INCOMPLETE_LANE_CHECKLIST.value

    def submit_company_search_result(self, status: str = "") -> dict:
        self.base.tool_calls += 1
        csr = self.base.build_result(model=self.model)
        csr.status = self._v4_status()
        csr.jobs = list(self.base.details)
        csr.tool_calls = self.tool_calls
        self.submitted = csr
        return {"accepted": True, "status": csr.status, "jobs": len(csr.jobs),
                "lanes_complete": csr.lane_checklist_complete(self.config.primary_lanes)}

    def build_result(self) -> CompanySearchResult:
        csr = self.base.build_result(model=self.model)
        csr.status = self._v4_status()
        csr.jobs = list(self.base.details)
        csr.tool_calls = self.tool_calls
        return csr

    def cleanup(self) -> None:
        try:
            self.browser_close()
        except Exception:  # noqa: BLE001
            pass


def CompanySearchStatus_legacy_access() -> tuple:
    from atlas.pilot.models import CompanyStatus
    return (CompanyStatus.ACCESS_LIMITED.value, CompanyStatus.AUTH_REQUIRED.value)


#: The exact set of V4 tool names the live agent may call.
V4_TOOL_NAMES = frozenset({
    "resolve_official_company_site", "discover_official_careers_entry", "search_official_career_site",
    "open_official_job_detail", "web_search_leads", "web_fetch_official",
    "browser_start", "browser_goto_search", "browser_observe", "browser_fill", "browser_click",
    "browser_press", "browser_select_option", "browser_wait", "browser_scroll_or_load_more",
    "browser_collect_job_cards", "browser_open_job_detail", "browser_back", "browser_close",
    "browser_search_lane", "submit_company_search_result",
})


def build_v4_sdk_tools(tb: AgenticCompanyToolbox) -> list[Any]:
    """Wrap the V4 toolbox methods as official Copilot SDK Tool objects (lazy SDK import)."""
    import json as _json

    from copilot import define_tool
    from pydantic import BaseModel, Field

    class ResolveParams(BaseModel):
        company: str = Field(default="")
        domain_hints: list[str] = Field(default_factory=list)

    class DiscoverParams(BaseModel):
        domain: str = Field(default="")
        entry_hint: str = Field(default="")

    class SearchParams(BaseModel):
        query: str = Field(description="Short role query, e.g. 'Java Developer'")
        lane: str = Field(default="", description="Target lane key")
        india_locations: list[str] = Field(default_factory=list)
        page_budget: int = Field(default=3)

    class DetailParams(BaseModel):
        job_url: str = Field(description="Validated official job detail URL")
        lane_hint: str = Field(default="")

    class WebSearchParams(BaseModel):
        query: str = Field(description="Public-web search query for official/ATS leads")
        site_scope: bool = Field(default=True)

    class WebFetchParams(BaseModel):
        url: str = Field(description="Official/ATS HTTPS URL to fetch read-only")

    class StartParams(BaseModel):
        url: str = Field(description="Validated official/ATS HTTPS career URL")

    class HandleParams(BaseModel):
        handle: str = Field(default="", description="Handle from the latest observation")

    class FillParams(BaseModel):
        handle: str = Field(description="Input handle from the latest observation")
        value: str = Field(description="Text to type")

    class PressParams(BaseModel):
        key: str = Field(description="Key to press, e.g. Enter")
        handle: str = Field(default="")

    class SelectParams(BaseModel):
        handle: str = Field(description="Select handle from the latest observation")
        value: str = Field(description="Option value or label")

    class WaitParams(BaseModel):
        ms: int = Field(default=800)
        state: str = Field(default="", description="load|domcontentloaded|networkidle or empty")

    class CollectParams(BaseModel):
        lane: str = Field(default="")

    class OpenDetailParams(BaseModel):
        handle: str = Field(default="", description="Job-card handle from the latest observation")
        url: str = Field(default="", description="Trusted job detail URL")
        lane_hint: str = Field(default="")

    class LaneSearchParams(BaseModel):
        lane: str = Field(description="Lane key this search serves")
        query: str = Field(description="Role query")
        india_location: str = Field(default="India")

    class EmptyParams(BaseModel):
        pass

    class SubmitParams(BaseModel):
        status: str = Field(default="")

    def _wrap(fn):
        def handler(params, _inv):
            kwargs = params.model_dump() if hasattr(params, "model_dump") else dict(params)
            try:
                return _json.dumps(fn(**kwargs), ensure_ascii=False, default=str)[:12000]
            except Exception as exc:  # noqa: BLE001
                return _json.dumps({"error": f"{type(exc).__name__}: {exc}"})
        return handler

    def T(name, desc, fn, params, terminal=False):
        return define_tool(name, description=desc, handler=_wrap(fn), params_type=params,
                           is_terminal=terminal, skip_permission=True)

    return [
        T("resolve_official_company_site", "Resolve the company's official career domain (HTTPS official only).",
          tb.resolve_official_company_site, ResolveParams),
        T("discover_official_careers_entry", "Fetch the official careers entry read-only and detect the ATS/route.",
          tb.discover_official_careers_entry, DiscoverParams),
        T("search_official_career_site", "Search the resolved official/ATS source for India roles (fast path).",
          tb.search_official_career_site, SearchParams),
        T("open_official_job_detail", "Open a validated official/ATS job detail URL and extract evidence.",
          tb.open_official_job_detail, DetailParams),
        T("web_search_leads", "Public-web search for OFFICIAL/ATS leads only (leads are validated in Python).",
          tb.web_search_leads, WebSearchParams),
        T("web_fetch_official", "Fetch an official/ATS page read-only (refused unless trusted).",
          tb.web_fetch_official, WebFetchParams),
        T("browser_start", "Open ONE persistent read-only browser session at an official/ATS career URL.",
          tb.browser_start, StartParams),
        T("browser_goto_search", "Navigate the persistent browser to a trusted search-results URL "
          "(the site's own ?q=&location= URL). Reliable for SPAs that filter via the URL; avoids clicks.",
          tb.browser_goto_search, StartParams),
        T("browser_observe", "Return a bounded structured observation of the current page (handles, cards, inputs).",
          tb.browser_observe, EmptyParams),
        T("browser_fill", "Type text into an input referenced by a handle from the latest observation.",
          tb.browser_fill, FillParams),
        T("browser_click", "Click a button/link referenced by a handle from the latest observation.",
          tb.browser_click, HandleParams),
        T("browser_press", "Press a key (optionally within a handle's input).", tb.browser_press, PressParams),
        T("browser_select_option", "Select a dropdown option by a handle from the latest observation.",
          tb.browser_select_option, SelectParams),
        T("browser_wait", "Wait for a load state or a bounded time.", tb.browser_wait, WaitParams),
        T("browser_scroll_or_load_more", "Scroll or click a load-more control (handle optional).",
          tb.browser_scroll_or_load_more, HandleParams),
        T("browser_collect_job_cards", "Collect the current visible job cards (title+location+handle).",
          tb.browser_collect_job_cards, CollectParams),
        T("browser_open_job_detail", "Open a job detail (by card handle or trusted url) and capture evidence.",
          tb.browser_open_job_detail, OpenDetailParams),
        T("browser_back", "Go back to the previous page in the persistent session.", tb.browser_back, EmptyParams),
        T("browser_close", "Close the persistent browser session.", tb.browser_close, EmptyParams),
        T("browser_search_lane", "One bounded lane search on the live page (fill+location+submit+observe+collect).",
          tb.browser_search_lane, LaneSearchParams),
        T("submit_company_search_result", "Submit the typed company result with the lane checklist. Call LAST.",
          tb.submit_company_search_result, SubmitParams, terminal=True),
    ]
