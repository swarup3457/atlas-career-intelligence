"""Bounded, read-only Playwright career-search interaction (architecture s.4, tool 5).

STRICTLY read-only: navigate an official career search page, optionally type a query / pick
an India location / submit / paginate / scroll, then scrape visible public job-link anchors.
It NEVER logs in, fills credentials, clicks Apply, executes model-supplied JavaScript, or
attempts to bypass a CAPTCHA / login wall / anti-bot control — a detected challenge is
reported truthfully as a limitation. Every call is hard-bounded in time and actions, and any
failure (including Playwright/browser unavailability) degrades to a truthful limitation.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlsplit

_NAV_TIMEOUT_MS = 15000
_MAX_ANCHORS = 60

_CHALLENGE_MARKERS = (
    "captcha", "are you a robot", "verify you are human", "access denied",
    "sign in", "log in", "login", "please enable javascript and cookies",
    "unusual traffic", "cf-challenge", "px-captcha", "incapsula",
)

_JOB_HREF_RE = re.compile(r"(job|career|requisition|posting|vacanc|opening)", re.I)
_INDIA_TOKENS = ("india", "bengaluru", "bangalore", "hyderabad", "pune", "chennai",
                 "noida", "gurugram", "gurgaon", "mumbai", "delhi")


def _india(text: str) -> bool:
    low = (text or "").lower()
    return any(t in low for t in _INDIA_TOKENS)


def bounded_browser_action(career_url: str, action: str, *, value: str = "", india_only: bool = True) -> dict:
    """Perform ONE bounded read-only browser action; return {cards, limitation, note}.

    The sync Playwright API cannot run inside a running asyncio event loop (the live SDK
    session), so the work is always executed in a dedicated worker thread with a hard time
    budget. Any failure degrades to a truthful limitation.
    """
    import concurrent.futures as _cf

    with _cf.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(_do_browser_action, career_url, action, value, india_only)
        try:
            return fut.result(timeout=60)
        except _cf.TimeoutError:
            return {"cards": [], "limitation": "browser action exceeded time budget"}
        except Exception as exc:  # noqa: BLE001
            return {"cards": [], "limitation": f"browser error: {type(exc).__name__}: {exc}"}


def _do_browser_action(career_url: str, action: str, value: str, india_only: bool) -> dict:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        return {"cards": [], "limitation": f"browser unavailable: {type(exc).__name__}"}

    host = (urlsplit(career_url).netloc or "").lower()
    cards: list[dict] = []
    limitation = ""
    note = ""
    try:
        with sync_playwright() as pw:
            browser = None
            for launch in ({"channel": "chrome", "headless": True}, {"headless": True}):
                try:
                    browser = pw.chromium.launch(**launch)
                    break
                except Exception:  # noqa: BLE001
                    continue
            if browser is None:
                return {"cards": [], "limitation": "no launchable Chromium/Chrome for read-only browsing"}
            ctx = browser.new_context(
                user_agent="Atlas-Career-Intelligence/1.0 (personal job-search research; read-only)",
                viewport={"width": 1280, "height": 900},
            )
            page = ctx.new_page()
            page.set_default_timeout(_NAV_TIMEOUT_MS)
            try:
                page.goto(career_url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
            except Exception as exc:  # noqa: BLE001
                browser.close()
                return {"cards": [], "limitation": f"navigation failed: {type(exc).__name__}"}

            body_text = (page.content() or "").lower()[:200000]
            if any(m in body_text for m in _CHALLENGE_MARKERS):
                browser.close()
                return {"cards": [], "limitation": "challenge/login/anti-bot detected; not bypassed"}

            try:
                if action == "fill_search_text" and value:
                    _fill_first(page, ["input[type=search]", "input[name*=search i]",
                                       "input[placeholder*=search i]", "input[aria-label*=search i]"], value)
                elif action == "choose_india_location" and value:
                    _fill_first(page, ["input[name*=location i]", "input[placeholder*=location i]",
                                       "input[aria-label*=location i]"], value)
                elif action == "submit_search":
                    _click_first(page, ["button[type=submit]", "button:has-text('Search')",
                                        "button:has-text('Find')", "[role=button]:has-text('Search')"])
                    page.wait_for_load_state("networkidle", timeout=_NAV_TIMEOUT_MS)
                elif action in ("next_page",):
                    _click_first(page, ["a:has-text('Next')", "button:has-text('Next')",
                                        "[aria-label*=next i]"])
                    page.wait_for_load_state("networkidle", timeout=_NAV_TIMEOUT_MS)
                elif action == "load_more":
                    _click_first(page, ["button:has-text('Load more')", "button:has-text('Show more')"])
                    page.wait_for_load_state("networkidle", timeout=_NAV_TIMEOUT_MS)
                elif action == "scroll":
                    page.mouse.wheel(0, 4000)
                    page.wait_for_timeout(800)
                elif action == "open_job_detail" and value:
                    note = "open_job_detail via browser is read-only navigation only"
            except Exception as exc:  # noqa: BLE001
                note = f"action degraded: {type(exc).__name__}"

            anchors = page.eval_on_selector_all(
                "a", "els => els.slice(0, 400).map(e => ({href: e.href, text: (e.textContent||'').trim()}))"
            ) or []
            seen = set()
            for a in anchors:
                href = a.get("href") or ""
                text = a.get("text") or ""
                if not href or not href.startswith("https://"):
                    continue
                if urlsplit(href).netloc.lower() != host and host not in urlsplit(href).netloc.lower():
                    continue
                if not (_JOB_HREF_RE.search(href) or _JOB_HREF_RE.search(text)):
                    continue
                if india_only and not (_india(text) or _india(href)):
                    continue
                if href in seen:
                    continue
                seen.add(href)
                cards.append({"title": text[:160], "location": "", "url": href,
                              "source_family": "OFFICIAL_CAREERS_BROWSER"})
                if len(cards) >= _MAX_ANCHORS:
                    break
            browser.close()
    except Exception as exc:  # noqa: BLE001
        return {"cards": cards, "limitation": f"browser error: {type(exc).__name__}: {exc}"}
    return {"cards": cards, "limitation": limitation, "note": note}


def _fill_first(page, selectors, value) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.fill(value, timeout=4000)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _click_first(page, selectors) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click(timeout=4000)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


__all__ = ["bounded_browser_action"]
