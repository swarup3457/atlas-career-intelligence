"""Atlas career-page worker.

A concrete BaseWorker implementation demonstrating end-to-end use of
BrowserManager + the centralized retry/terminal-status model. This is
intentionally generic — it accepts a mapping of company name -> entry URL
supplied by the caller, and performs a lightweight, read-only page check
(reachability, title, optional keyword search). It does NOT encode any
final Atlas business rules (search lanes, ATS-specific adapters,
candidate matching, etc.) — those arrive with the Workspace Agent import.

Extraction uncertainty is reported as EXTRACTION_UNRESOLVED, never
silently coerced into NO_RELEVANT_RESULTS (see docs/STATE_MODEL.md).
"""

from __future__ import annotations

import re
from typing import Optional

from playwright.sync_api import Page

from atlas.browser.manager import BrowserManager
from atlas.models import ErrorCategory, TaskStatus
from atlas.workers.base import BaseWorker, WorkerError, WorkerOutcome

SEARCH_INPUT_SELECTORS = [
    "input[placeholder*='job' i]",
    "input[placeholder*='keyword' i]",
    "input[aria-label*='search' i]",
    "input[name*='query' i]",
    "input[name='search']",
    "input[type='search']",
]

JOB_LINK_PATTERN = re.compile(
    r"(job[-_]?detail|/jobs?/\d|/job/(?!categor)|jobid=|req(uisition)?id=|"
    r"/job-postings?/|/careers/jobdetail|position[-_]?detail)",
    re.IGNORECASE,
)

CONSENT_BUTTON_SELECTORS = [
    "#onetrust-accept-btn-handler",
    "button:has-text('Accept All')",
    "button:has-text('Accept all')",
    "button:has-text('I Accept')",
]


class CareerPageWorker(BaseWorker):
    """Performs one lightweight, read-only career-page check per attempt."""

    name = "career_page"

    def __init__(
        self,
        browser_manager: BrowserManager,
        entry_urls: dict[str, str],
        search_keywords: Optional[str] = None,
        headless: bool = True,
        simulated_first_attempt_failures: Optional[set[str]] = None,
    ):
        self.browser_manager = browser_manager
        self.entry_urls = entry_urls
        self.search_keywords = search_keywords
        self.headless = headless
        self.simulated_first_attempt_failures = simulated_first_attempt_failures or set()
        self._page: Optional[Page] = None

    def _get_page(self) -> Page:
        if self._page is None:
            self._page = self.browser_manager.launch(headless=self.headless)
        return self._page

    def attempt(self, item: str, attempt_number: int) -> WorkerOutcome:
        if item in self.simulated_first_attempt_failures and attempt_number == 1:
            raise WorkerError(
                ErrorCategory.INTENTIONAL_TEST_FAILURE,
                f"Simulated deterministic failure for {item} (test-only).",
            )

        entry_url = self.entry_urls.get(item)
        if entry_url is None:
            raise WorkerError(
                ErrorCategory.UNKNOWN, f"No entry URL configured for '{item}'."
            )

        page = self._get_page()

        try:
            response = self.browser_manager.navigate(page, entry_url)
        except Exception as exc:  # noqa: BLE001
            raise WorkerError(ErrorCategory.TRANSIENT_NAVIGATION, f"Navigation error: {exc}") from exc

        status_code = response.status if response is not None else None
        limitation = self.browser_manager.detect_access_limitation(page)
        if limitation or (status_code is not None and status_code in (401, 403, 429, 503)):
            raise WorkerError(
                ErrorCategory.ANTI_BOT,
                limitation or f"HTTP {status_code} received from career site.",
            )

        for consent_selector in CONSENT_BUTTON_SELECTORS:
            try:
                btn = page.locator(consent_selector).first
                if btn.count() > 0 and btn.is_visible(timeout=1000):
                    btn.click(timeout=1500)
                    page.wait_for_timeout(500)
                    break
            except Exception:  # noqa: BLE001
                continue

        search_interface_accessible = False
        if self.search_keywords:
            for selector in SEARCH_INPUT_SELECTORS:
                try:
                    matches = page.locator(selector)
                    visible_locator = None
                    for idx in range(min(matches.count(), 5)):
                        candidate = matches.nth(idx)
                        if candidate.is_visible(timeout=800):
                            visible_locator = candidate
                            break
                    if visible_locator is None:
                        continue
                    visible_locator.click(timeout=2000)
                    visible_locator.fill(self.search_keywords, timeout=2000)
                    visible_locator.press("Enter", timeout=2000)
                    search_interface_accessible = True
                    page.wait_for_timeout(3000)
                    break
                except Exception:  # noqa: BLE001
                    continue

        post_limitation = self.browser_manager.detect_access_limitation(page)
        if post_limitation:
            raise WorkerError(ErrorCategory.ANTI_BOT, post_limitation)

        try:
            title = page.title()
        except Exception:  # noqa: BLE001
            # Page reachable but title unreadable - this is extraction
            # uncertainty, NOT "no relevant results".
            return WorkerOutcome(
                status=TaskStatus.EXTRACTION_UNRESOLVED,
                payload={
                    "official_careers_url": entry_url,
                    "final_url": getattr(page, "url", None),
                    "reason": "Could not read page title after navigation.",
                },
            )

        try:
            candidates = page.eval_on_selector_all(
                "a[href]",
                "els => els.map(e => ({t: (e.innerText || '').trim(), h: e.href}))",
            )
        except Exception as exc:  # noqa: BLE001
            return WorkerOutcome(
                status=TaskStatus.EXTRACTION_UNRESOLVED,
                payload={
                    "official_careers_url": entry_url,
                    "final_url": page.url,
                    "page_title": title,
                    "search_interface_accessible": search_interface_accessible,
                    "reason": f"Link extraction raised: {exc}",
                },
            )

        seen_urls: set[str] = set()
        sample_jobs = []
        for cand in candidates:
            href = cand.get("h", "")
            text = cand.get("t", "")
            if not href or not text or len(text) < 4:
                continue
            if not JOB_LINK_PATTERN.search(href):
                continue
            if href in seen_urls:
                continue
            seen_urls.add(href)
            if len(sample_jobs) < 5:
                sample_jobs.append({"title": text[:150], "url": href})

        status = TaskStatus.SUCCESS if sample_jobs else TaskStatus.NO_RELEVANT_RESULTS

        return WorkerOutcome(
            status=status,
            payload={
                "official_careers_url": entry_url,
                "final_url": page.url,
                "page_title": title,
                "search_interface_accessible": search_interface_accessible,
                "visible_job_count": len(seen_urls),
                "sample_jobs": sample_jobs,
            },
        )
