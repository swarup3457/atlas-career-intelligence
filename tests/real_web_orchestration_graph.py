"""Atlas Test 3 — LangGraph + Playwright real-web orchestration graph.

This module defines:
    - The persisted LangGraph state schema.
    - A deterministic, LangGraph-driven queue/retry state machine.
    - A Playwright-backed worker that performs a lightweight, read-only
      public career-page check for one company per invocation.

Scope guardrails (intentional, do not expand):
    - No LLM/Copilot calls.
    - No login automation, no credential handling.
    - No CAPTCHA/MFA/anti-bot/Zscaler/security bypass of any kind.
    - No aggressive scraping - a single page load plus, at most, one
      lightweight keyword search attempt per company.
    - No application flow is ever triggered.
"""

from __future__ import annotations

import datetime
import re
from typing import Any, Optional, TypedDict

from langgraph.graph import END, StateGraph

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

THREAD_ID = "atlas-real-web-test"

MAX_RETRIES = 2  # transient browser/navigation failures only

SEARCH_KEYWORDS = "Software Engineer"

# Official public careers/search entry points, discovered by following each
# company's own "Careers" / "Job search" navigation (not hardcoded job
# posting URLs, which are expected to expire).
COMPANY_ENTRY_URLS: dict[str, str] = {
    "Microsoft": "https://careers.microsoft.com/v2/global/en/home.html",
    "Amazon": "https://www.amazon.jobs/en/",
    "Accenture": "https://www.accenture.com/us-en/careers/jobsearch",
    # Discovered by following the official Deloitte careers page's own
    # "Search jobs" link (https://www.deloitte.com/us/en/careers/careers.html)
    # rather than a hardcoded job posting URL.
    "Deloitte": "https://apply.deloitte.com/careers/SearchJobs?sort=relevancy",
    "ServiceNow": "https://careers.servicenow.com/jobs/",
}

COMPANIES = list(COMPANY_ENTRY_URLS.keys())

# Company that must have its FIRST attempt intentionally fail, BEFORE any
# navigation happens, to prove LangGraph retry logic works even with real
# Playwright workers in the loop.
SIMULATED_FIRST_FAILURE_COMPANY = "Deloitte"

# Terminal statuses a company can reach.
STATUS_SUCCESS = "SUCCESS"
STATUS_NO_RELEVANT_RESULTS = "NO_RELEVANT_RESULTS"
STATUS_ACCESS_LIMITED = "ACCESS_LIMITED"
STATUS_PERMANENT_FAILURE = "PERMANENT_FAILURE_AFTER_RETRIES"
TERMINAL_STATUSES = {
    STATUS_SUCCESS,
    STATUS_NO_RELEVANT_RESULTS,
    STATUS_ACCESS_LIMITED,
    STATUS_PERMANENT_FAILURE,
}

RUN_STATUS_RUNNING = "RUNNING"
RUN_STATUS_COMPLETE = "COMPLETE"

# Signals that indicate the site is deliberately blocking automated access.
# When detected, we classify ACCESS_LIMITED immediately and do NOT retry -
# retrying against an anti-bot/security control would be an attempted
# bypass, which is explicitly prohibited.
ACCESS_LIMITATION_SIGNALS = [
    "captcha",
    "are you a human",
    "verify you are a human",
    "verify you're human",
    "unusual traffic",
    "access denied",
    "access to this page has been denied",
    "just a moment",  # Cloudflare interstitial challenge title
    "checking your browser",
    "perimeterx",
    "please enable javascript and cookies",
    "bot detection",
    "zscaler",
    "blocked by policy",
    "forbidden",
]

# Known ATS/career-platform domain fragments for lightweight identification.
ATS_DOMAIN_HINTS = [
    ("myworkdayjobs.com", "Workday"),
    ("greenhouse.io", "Greenhouse"),
    ("lever.co", "Lever"),
    ("icims.com", "iCIMS"),
    ("successfactors", "SAP SuccessFactors"),
    ("taleo.net", "Oracle Taleo"),
    ("smartrecruiters.com", "SmartRecruiters"),
    ("eightfold.ai", "Eightfold AI"),
    ("phenompeople.com", "Phenom People"),
    ("phenom.com", "Phenom People"),
    ("avature.net", "Avature"),
    ("jobvite.com", "Jobvite"),
    ("brassring.com", "BrassRing (IBM Kenexa)"),
    ("oraclecloud.com", "Oracle Cloud Recruiting"),
    ("apply.deloitte.com", "Deloitte proprietary careers platform"),
    ("amazon.jobs", "Amazon Jobs (proprietary platform)"),
    ("careers.microsoft.com", "Microsoft Careers (proprietary platform)"),
    ("accenture.com", "Accenture Careers (proprietary platform)"),
    ("careers.servicenow.com", "ServiceNow Careers (proprietary platform)"),
]

# Generic selectors used, in order, to find a keyword search box on a
# career site's landing/search page. Best-effort only - if none match, the
# company is reported with search_interface_accessible = False rather than
# treated as an error.
SEARCH_INPUT_SELECTORS = [
    "input[placeholder*='job' i]",
    "input[placeholder*='keyword' i]",
    "input[aria-label*='search' i]",
    "input[name*='query' i]",
    "input[name='search']",
    "input[type='search']",
]

# Generic href patterns that suggest a link points at an individual job
# posting (used only to COUNT/extract already-public job listing links on
# a page the company has already made publicly browsable - not a scrape
# of restricted content).
JOB_LINK_PATTERN = re.compile(
    r"(job[-_]?detail|/jobs?/\d|/job/(?!categor)|jobid=|req(uisition)?id=|"
    r"/job-postings?/|/careers/jobdetail|position[-_]?detail)",
    re.IGNORECASE,
)


class AtlasRealWebState(TypedDict):
    planned_companies: list[str]
    completed_companies: list[str]
    remaining_companies: list[str]
    retry_counts: dict[str, int]
    failed_companies: list[str]
    access_limited_companies: list[str]
    company_results: dict[str, dict[str, Any]]
    attempt_log: list[dict[str, Any]]
    current_company: Optional[str]
    run_status: str


def initial_state() -> AtlasRealWebState:
    return AtlasRealWebState(
        planned_companies=list(COMPANIES),
        completed_companies=[],
        remaining_companies=list(COMPANIES),
        retry_counts={},
        failed_companies=[],
        access_limited_companies=[],
        company_results={},
        attempt_log=[],
        current_company=None,
        run_status=RUN_STATUS_RUNNING,
    )


def _identify_platform(url: str) -> str:
    url_l = url.lower()
    for fragment, label in ATS_DOMAIN_HINTS:
        if fragment in url_l:
            return label
    return "Unknown/Custom"


def _detect_access_limitation(title: str, body_text: str) -> Optional[str]:
    haystack = f"{title}\n{body_text}".lower()
    for signal in ACCESS_LIMITATION_SIGNALS:
        if signal in haystack:
            return f"Detected access-limitation signal: '{signal}'"
    return None


class TransientWorkerFailure(Exception):
    """Raised for ordinary, retryable browser/navigation failures."""


class AccessLimitedError(Exception):
    """Raised when a security/anti-bot control is detected. Not retried."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _perform_company_check(page, company: str, attempt_number: int) -> dict[str, Any]:
    """Perform ONE lightweight, read-only career-page check for a company.

    Returns a dict describing the outcome. Raises TransientWorkerFailure for
    ordinary retryable errors, or AccessLimitedError when an explicit
    security/anti-bot control is detected (never retried).
    """
    # --- Intentional deterministic test failure (proves retry logic) ---
    if company == SIMULATED_FIRST_FAILURE_COMPANY and attempt_number == 1:
        raise TransientWorkerFailure(
            f"Simulated deterministic failure for {company} "
            "(forced before navigation, for retry-logic testing)."
        )

    entry_url = COMPANY_ENTRY_URLS[company]

    try:
        response = page.goto(entry_url, wait_until="load", timeout=30000)
    except Exception as exc:  # noqa: BLE001
        raise TransientWorkerFailure(f"Navigation error: {exc}") from exc

    final_url = page.url
    try:
        title = page.title()
    except Exception:  # noqa: BLE001
        title = ""
    try:
        body_text = page.inner_text("body")
    except Exception:  # noqa: BLE001
        body_text = ""

    status_code = response.status if response is not None else None

    limitation = _detect_access_limitation(title, body_text)
    if limitation or (status_code is not None and status_code in (401, 403, 429, 503)):
        raise AccessLimitedError(
            limitation or f"HTTP {status_code} received from career site."
        )

    platform = _identify_platform(final_url)

    # Dismiss common, non-security cookie-consent banners (e.g. OneTrust)
    # if present, purely so they do not intercept clicks on the page's own
    # search box. This is a routine consent dismissal, not a bypass of any
    # security/anti-bot/CAPTCHA/MFA control.
    for consent_selector in [
        "#onetrust-accept-btn-handler",
        "button:has-text('Accept All')",
        "button:has-text('Accept all')",
        "button:has-text('I Accept')",
    ]:
        try:
            btn = page.locator(consent_selector).first
            if btn.count() > 0 and btn.is_visible(timeout=1000):
                btn.click(timeout=1500)
                page.wait_for_timeout(500)
                break
        except Exception:  # noqa: BLE001
            continue

    # --- Lightweight, best-effort keyword search attempt ---
    search_interface_accessible = False
    for selector in SEARCH_INPUT_SELECTORS:
        try:
            matches = page.locator(selector)
            match_count = matches.count()
            visible_locator = None
            for idx in range(min(match_count, 5)):
                candidate = matches.nth(idx)
                if candidate.is_visible(timeout=800):
                    visible_locator = candidate
                    break
            if visible_locator is None:
                continue
            visible_locator.click(timeout=2000)
            visible_locator.fill(SEARCH_KEYWORDS, timeout=2000)
            visible_locator.press("Enter", timeout=2000)
            search_interface_accessible = True
            page.wait_for_timeout(3000)
            break
        except Exception:  # noqa: BLE001
            continue

    # Re-check for access limitation after search interaction (in case a
    # search attempt triggered a challenge page).
    try:
        post_title = page.title()
        post_body = page.inner_text("body")
    except Exception:  # noqa: BLE001
        post_title, post_body = title, body_text
    post_limitation = _detect_access_limitation(post_title, post_body)
    if post_limitation:
        raise AccessLimitedError(post_limitation)

    # --- Extract up to 5 visible job title/url pairs, best-effort ---
    sample_jobs: list[dict[str, str]] = []
    visible_job_count = 0
    try:
        candidates = page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => ({t: (e.innerText || '').trim(), h: e.href}))",
        )
    except Exception:  # noqa: BLE001
        candidates = []

    seen_urls: set[str] = set()
    for item in candidates:
        href = item.get("h", "")
        text = item.get("t", "")
        if not href or not text or len(text) < 4:
            continue
        if not JOB_LINK_PATTERN.search(href):
            continue
        if href in seen_urls:
            continue
        seen_urls.add(href)
        if len(sample_jobs) < 5:
            sample_jobs.append({"title": text[:150], "url": href})
    visible_job_count = len(seen_urls)

    outcome_status = STATUS_SUCCESS if sample_jobs else STATUS_NO_RELEVANT_RESULTS

    return {
        "status": outcome_status,
        "official_careers_url": entry_url,
        "final_url": final_url,
        "page_title": post_title or title,
        "career_platform": platform,
        "search_interface_accessible": search_interface_accessible,
        "visible_job_count": visible_job_count,
        "sample_jobs": sample_jobs,
        "access_limitation": None,
        "error": None,
    }


def make_process_one(page):
    """Build the single LangGraph node, bound to a live Playwright page."""

    def process_one(state: AtlasRealWebState) -> AtlasRealWebState:
        remaining = list(state.get("remaining_companies", []))
        completed = list(state.get("completed_companies", []))
        retry_counts = dict(state.get("retry_counts", {}))
        failed = list(state.get("failed_companies", []))
        access_limited = list(state.get("access_limited_companies", []))
        company_results = dict(state.get("company_results", {}))
        attempt_log = list(state.get("attempt_log", []))

        if not remaining:
            return {**state, "current_company": None, "run_status": RUN_STATUS_COMPLETE}

        company = remaining.pop(0)
        attempt_number = retry_counts.get(company, 0) + 1
        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()

        terminal_status: Optional[str] = None
        result_payload: dict[str, Any] = {}
        attempt_outcome = "UNKNOWN"

        try:
            result_payload = _perform_company_check(page, company, attempt_number)
            terminal_status = result_payload["status"]
            attempt_outcome = terminal_status
        except AccessLimitedError as exc:
            terminal_status = STATUS_ACCESS_LIMITED
            attempt_outcome = STATUS_ACCESS_LIMITED
            result_payload = {
                "status": STATUS_ACCESS_LIMITED,
                "official_careers_url": COMPANY_ENTRY_URLS.get(company),
                "final_url": None,
                "page_title": None,
                "career_platform": None,
                "search_interface_accessible": False,
                "visible_job_count": 0,
                "sample_jobs": [],
                "access_limitation": exc.message,
                "error": None,
            }
        except TransientWorkerFailure as exc:
            attempt_outcome = "TRANSIENT_FAILURE"
            if attempt_number > MAX_RETRIES:
                terminal_status = STATUS_PERMANENT_FAILURE
                result_payload = {
                    "status": STATUS_PERMANENT_FAILURE,
                    "official_careers_url": COMPANY_ENTRY_URLS.get(company),
                    "final_url": None,
                    "page_title": None,
                    "career_platform": None,
                    "search_interface_accessible": False,
                    "visible_job_count": 0,
                    "sample_jobs": [],
                    "access_limitation": None,
                    "error": str(exc),
                }
            else:
                terminal_status = None  # requeue, not yet terminal
                result_payload = {"error": str(exc)}

        attempt_log.append(
            {
                "company": company,
                "attempt_number": attempt_number,
                "result": attempt_outcome,
                "timestamp": timestamp,
            }
        )
        retry_counts[company] = attempt_number

        if terminal_status is None:
            # Transient failure, still within retry budget - requeue.
            remaining.append(company)
        else:
            company_results[company] = {"attempts": attempt_number, **result_payload}
            completed.append(company)
            if terminal_status == STATUS_ACCESS_LIMITED:
                access_limited.append(company)
            elif terminal_status == STATUS_PERMANENT_FAILURE:
                failed.append(company)

        run_status = RUN_STATUS_COMPLETE if not remaining else RUN_STATUS_RUNNING

        return {
            "planned_companies": state.get("planned_companies", list(COMPANIES)),
            "completed_companies": completed,
            "remaining_companies": remaining,
            "retry_counts": retry_counts,
            "failed_companies": failed,
            "access_limited_companies": access_limited,
            "company_results": company_results,
            "attempt_log": attempt_log,
            "current_company": company,
            "run_status": run_status,
        }

    return process_one


def build_graph(page):
    """Build (uncompiled) the single-node real-web orchestration graph."""
    builder = StateGraph(AtlasRealWebState)
    builder.add_node("process_one", make_process_one(page))
    builder.set_entry_point("process_one")
    builder.add_edge("process_one", END)
    return builder
