# BROWSER_STRATEGY

Follow this ladder in order. Never repeatedly retry the same blocked click.

1. **Structured first.** If the site clearly exposes a structured ATS/API result
   (Greenhouse, Lever, Ashby, Workday), prefer it.
2. **Reuse a validated recipe** if one is provided in the prompt.
3. **Open the official career SEARCH page** for the company.
4. **Prefer URL query/location parameters** (keyword + India location) over UI
   clicks — set them in the URL when the site supports it.
5. **Observe the REAL result state**: results listed, a real zero-results state,
   or an external block. Do not infer results you did not see.
6. **Inspect DOM anchors / data-attributes** for canonical job-detail URLs
   (e.g. `/careers/jobdetails/...`). This is the strategy that works on
   enterprise SPAs where overlay/card clicks are unreliable.
7. **Navigate DIRECTLY to canonical job-detail URLs.**
8. Use a normal accessible click **only** when no direct URL is available.
9. Use focused Playwright code **only** as a bounded last resort.
10. Never loop a blocked click strategy — change approach or stop truthfully.
11. **Capture exact detail evidence**: exact title, exact location, full JD text,
    verbatim experience text, and the stack, from the detail page you opened.
12. **Stop truthfully.** A job-card summary is not enough for an accepted job.

## External blocks

If the official site is genuinely blocked (confirmed login wall with a real
credential form and no visible job content, or an HTTP 401/403/429/503 with no
content), capture that as a browser-confirmed external block with evidence — do
not claim a block you did not observe. A normal header/footer "Sign in" link on a
populated results page is **not** a login wall.
