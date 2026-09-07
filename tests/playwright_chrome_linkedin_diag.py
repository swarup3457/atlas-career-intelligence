"""Atlas diagnostic (temporary): Is LinkedIn blocked in Playwright's bundled
Chromium only, or also in the installed Google Chrome (channel="chrome")?

This script does NOT implement any Atlas logic. It only:
  1. Launches a persistent context using the installed Google Chrome
     (channel="chrome") with a dedicated, throwaway profile.
  2. Navigates to https://example.com to confirm the browser works.
  3. Navigates once to https://www.linkedin.com/ and reports whether it
     loads, redirects, errors, or is blocked (e.g. by Zscaler).

No login automation, no credentials, no bypass of any security control.
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE_DIR = Path(r"C:\Atlas\.browser-profile-chrome")
EXAMPLE_URL = "https://example.com"
LINKEDIN_URL = "https://www.linkedin.com/"


def main() -> int:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    example_ok = False
    linkedin_status = "FAIL"
    linkedin_error = ""

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chrome",
            headless=False,
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()

            print(f"=== Navigating to {EXAMPLE_URL} ===")
            try:
                resp = page.goto(EXAMPLE_URL, wait_until="load", timeout=30000)
                if resp is not None and resp.ok:
                    example_ok = True
                    print(f"[PASS] example.com loaded (HTTP {resp.status}), title='{page.title()}'")
                else:
                    print(f"[FAIL] example.com returned: {resp.status if resp else 'no response'}")
            except Exception as exc:  # noqa: BLE001
                print(f"[FAIL] example.com navigation error: {exc}")

            print(f"\n=== Navigating to {LINKEDIN_URL} ===")
            try:
                resp = page.goto(LINKEDIN_URL, wait_until="load", timeout=30000)
                final_url = page.url
                title = page.title()
                status = resp.status if resp else None
                body_snippet = ""
                try:
                    body_snippet = page.inner_text("body")[:300]
                except Exception:  # noqa: BLE001
                    pass

                print(f"HTTP status: {status}")
                print(f"Final URL: {final_url}")
                print(f"Title: {title}")
                print(f"Body snippet: {body_snippet!r}")

                if "zscaler" in body_snippet.lower() or "zscaler" in title.lower():
                    linkedin_status = "BLOCKED"
                    linkedin_error = "Zscaler block page detected in response body/title."
                elif status is not None and status >= 400:
                    linkedin_status = "BLOCKED"
                    linkedin_error = f"HTTP {status} response."
                elif "linkedin.com" in final_url:
                    linkedin_status = "PASS"
                else:
                    linkedin_status = "FAIL"
                    linkedin_error = "Unexpected final URL/state."
            except Exception as exc:  # noqa: BLE001
                linkedin_status = "BLOCKED"
                linkedin_error = str(exc)
                print(f"[ERROR] LinkedIn navigation raised exception: {exc}")

            print(f"\nLINKEDIN_INSTALLED_CHROME_TEST={linkedin_status}")
            if linkedin_error:
                print(f"Error detail: {linkedin_error}")

            if linkedin_status == "PASS":
                print(
                    "\nLinkedIn loaded successfully. No login will be automated.\n"
                    f"Profile path: {PROFILE_DIR}\n"
                )
                try:
                    input(">>> Press ENTER to close the browser... ")
                except EOFError:
                    print("(No interactive input available - closing automatically.)")
            else:
                print(
                    "\nLinkedIn did not load successfully in installed Chrome either.\n"
                    "No workaround will be attempted."
                )
                try:
                    input(">>> Press ENTER to close the browser... ")
                except EOFError:
                    print("(No interactive input available - closing automatically.)")
        finally:
            context.close()

    print("\n=== DIAGNOSTIC SUMMARY ===")
    print(f"example.com: {'PASS' if example_ok else 'FAIL'}")
    print(f"linkedin.com: {linkedin_status}")
    if linkedin_error:
        print(f"exact error: {linkedin_error}")
    print(f"profile path: {PROFILE_DIR}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
