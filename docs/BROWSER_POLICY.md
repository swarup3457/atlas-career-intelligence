# Atlas Browser Policy

## Default execution mode: BACKGROUND / headless

Normal Atlas execution (career-page checks, ATS/portal lookups once those
adapters exist) MUST default to:

```python
BrowserManager(profile_dir=settings.browser_profile, channel="chrome")
manager.launch(headless=True)
```

This machine is also used for normal work, so Atlas must not repeatedly
pop visible Chrome windows onto the desktop during ordinary operation.

## Why `channel="chrome"` instead of bundled Chromium

Discovered in an earlier session (preserved, not re-litigated here):
Playwright's bundled Chromium is blocked on this network by a corporate
Zscaler proxy (raw HTTP probe showed `Server: Zscaler/6.2`, HTTP 403),
while the installed Google Chrome, launched via Playwright's
`channel="chrome"`, is not blocked. `BrowserManager` therefore always uses
`channel="chrome"` by default and no code path launches bundled Chromium
for real navigation.

## Headless + authenticated persistent profile — TESTED, not assumed

The spec explicitly required proving (not assuming) that
`headless=True` + `launch_persistent_context` can reuse an authenticated
session in the dedicated Atlas profile without corrupting or logging out
the profile.

**Result: PROVEN SAFE.** See `tests/test_headless_profile_diagnostic.py`:

- Launched headless against `C:\Atlas\.browser-profile-chrome`.
- Confirmed the LinkedIn feed page title (`"Feed | LinkedIn"`) — i.e. the
  profile's authenticated session was reused, not dropped, while headless.
- Closed the context cleanly, then relaunched headless again against the
  same profile — the same authenticated state was present, confirming no
  corruption occurred across a close/relaunch cycle.
- Attempted a second concurrent launch against the same profile while the
  first was open — `ProfileLockedError` was correctly raised, proving two
  Chrome instances are never attached to the same user-data-dir at once.

**Conclusion:** `HEADLESS_PROFILE_DIAGNOSTIC=PASS`. Background/headless is
confirmed as the safe default execution policy for this profile on this
machine, with the profile-lock mechanism protecting against concurrent
access. If Chrome/Zscaler behavior on this machine ever changes, rerun
`tests/test_headless_profile_diagnostic.py` before trusting this
conclusion again.

## Profile lock mechanism

`BrowserManager` writes a small lock file
(`.atlas-browser-manager.lock`) inside the profile directory whenever it
attaches to it, and removes it on `close()`. A second `BrowserManager`
instance (this or another process) attempting to attach to the same
profile while the lock file is present and recent (< 6 hours old) raises
`ProfileLockedError` instead of launching a second Chrome instance against
the same user-data-dir (which Chrome does not support safely). This is a
simple, single-developer-machine solution — not a distributed lock — and
is documented as such; see `atlas/browser/manager.py`.

Workers must never construct their own `playwright`/Chrome launch calls
directly — everything flows through `BrowserManager` so this invariant
holds everywhere.

## Visible escalation (the ONLY sanctioned way a visible window appears)

When a task genuinely needs a human — `LOGIN_REQUIRED`, `SESSION_EXPIRED`,
`MFA_REQUIRED`, `CAPTCHA_PRESENT`, `MANUAL_AUTH_REQUIRED`, or another
legitimate human-authentication requirement — the orchestration layer
calls `atlas.browser.intervention.escalate_for_human(...)`, which:

1. Launches a **visible** (`headless=False`) Chrome window using the same
   dedicated Atlas profile.
2. Prints/notifies the user which site needs attention and why.
3. Waits for an injectable `wait_for_user()` confirmation callback (in
   production this wraps a terminal `input()` prompt; in tests it is a
   fake callable, so the mechanism itself is testable without a real
   human — see `tests/test_foundation_suite.py`, section H).
4. Never reads, logs, or stores credentials at any point.
5. Re-checks session state (title/URL/status only) after confirmation.
6. Always closes the visible browser afterward (in a `finally` block),
   returning control to background/headless execution.

This is IMPLEMENTED and unit-tested with a simulated `LOGIN_REQUIRED`
condition (`tests/test_foundation_suite.py` section H). It has not yet
been exercised with a *real* human-in-the-loop MFA/CAPTCHA flow in this
build — that remains a natural next validation step once real
authentication-gated sources are added.

## Access-limitation detection

`BrowserManager.detect_access_limitation(page)` checks page title/body
text against a documented list of known blocking signals (CAPTCHA
prompts, "just a moment" / Cloudflare challenge text, Zscaler policy
block pages, PerimeterX, generic "access denied", etc.) — see
`ACCESS_LIMITATION_SIGNALS` in `atlas/browser/manager.py`. Detecting one
of these must always lead to a truthful `ACCESS_LIMITED` classification,
never a bypass attempt. This exact mechanism was validated against a real
Cloudflare-style ServiceNow challenge page in
`tests/real_web_orchestration_run.py`.
