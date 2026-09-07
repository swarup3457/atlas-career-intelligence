"""Atlas diagnostic (temporary): Verify that the dedicated Atlas Chrome
profile (C:\\Atlas\\.browser-profile-chrome) preserves a manually
authenticated LinkedIn session across a full browser restart.

This script does NOT:
  - automate login
  - read, print, log, or store any username/password
  - extract authentication cookies/tokens
  - bypass CAPTCHA, MFA, Zscaler, or any security control
  - scrape LinkedIn jobs/data

It only observes safe, page-level signals (current URL, page title, and
presence/absence of a "Sign in" control) to infer whether the browser
*appears* authenticated. No credential material is ever touched.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE_DIR = Path(r"C:\Atlas\.browser-profile-chrome")
LINKEDIN_URL = "https://www.linkedin.com/"


def looks_authenticated(page) -> str:
    """Return 'YES' or 'NO_OR_UNKNOWN' using only safe page-level signals."""
    try:
        url = page.url
        title = page.title()
    except Exception:  # noqa: BLE001
        return "NO_OR_UNKNOWN"

    url_l = url.lower()
    title_l = title.lower()

    # Logged-out LinkedIn typically stays on "/" or redirects to /login and
    # shows a title like "LinkedIn: Log In or Sign Up".
    if "/login" in url_l or "log in or sign up" in title_l:
        return "NO_OR_UNKNOWN"

    # Logged-in LinkedIn typically redirects to /feed (the home feed) after
    # opening "/".
    if "/feed" in url_l:
        return "YES"

    # Fall back to checking for a visible "Sign in" link, which is present
    # for logged-out visitors on the homepage.
    try:
        sign_in_visible = page.locator("a:has-text('Sign in')").first.is_visible(timeout=2000)
    except Exception:  # noqa: BLE001
        sign_in_visible = None

    if sign_in_visible is True:
        return "NO_OR_UNKNOWN"
    if sign_in_visible is False:
        return "YES"

    return "NO_OR_UNKNOWN"


def wait_enter(prompt: str) -> None:
    try:
        input(prompt)
    except EOFError:
        print("(No interactive input available - continuing automatically.)")


def main() -> int:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []

    # ---------------- PHASE 1 ----------------
    phase1_result = "NO_OR_UNKNOWN"
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chrome",
            headless=False,
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(LINKEDIN_URL, wait_until="load", timeout=30000)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"Phase 1 navigation error: {exc}")

            print("\nMANUAL_LOGIN_PHASE")
            print("Please manually log into LinkedIn in the visible Atlas Chrome window.")
            print("When login is complete, return to this terminal and press ENTER.")
            wait_enter(">>> Press ENTER once you have finished (or to skip)... ")

            phase1_result = looks_authenticated(page)
            print(f"PHASE1_AUTHENTICATED={phase1_result}")
        finally:
            context.close()

    # ---------------- Between phases ----------------
    print("\nClosed browser context. Waiting 5 seconds before restart...")
    time.sleep(5)

    # ---------------- PHASE 2 ----------------
    phase2_result = "FAIL_OR_UNKNOWN"
    fresh_login_requested = "UNKNOWN"
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chrome",
            headless=False,
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(LINKEDIN_URL, wait_until="load", timeout=30000)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"Phase 2 navigation error: {exc}")

            auth_state = looks_authenticated(page)
            if auth_state == "YES":
                phase2_result = "PASS"
                fresh_login_requested = "NO"
            else:
                phase2_result = "FAIL_OR_UNKNOWN"
                fresh_login_requested = "YES_OR_UNKNOWN"

            print(f"SESSION_PERSISTENCE={phase2_result}")

            wait_enter(">>> Press ENTER to close the browser... ")
        finally:
            context.close()

    print("\n=== FINAL REPORT ===")
    print("Browser channel used: chrome")
    print(f"Profile path: {PROFILE_DIR}")
    print(f"Phase 1 authentication detected: {phase1_result}")
    print("Browser successfully closed/reopened: YES")
    print(f"Phase 2 session retained: {phase2_result}")
    print(f"Fresh credentials requested after restart: {fresh_login_requested}")
    print("MFA/CAPTCHA/session-expiration observed: none detected by this script (not probed)")
    if errors:
        print("Errors:")
        for e in errors:
            print(f"  - {e}")
    else:
        print("Errors: none")

    return 0


if __name__ == "__main__":
    sys.exit(main())
