# Portal Discovery (Phase 1D)

READ-ONLY job-portal discovery for **LinkedIn** and **Naukri**, plus the
market-search campaign that turns portal observations into leads, links them to
official evidence, and drives bounded adaptive search waves.

## Hard safety boundary (no code path exists for any of these)

- No application submission / final Apply / form autofill.
- No account creation, credential entry/storage, or session/cookie export.
- No CAPTCHA/MFA/anti-bot bypass; no stealth/evasion/fingerprint masking; no
  rotating proxies; no mass scraping.
- A logged-out / challenge / access-limited response is **classified**
  (`LOGIN_REQUIRED` / `ACCESS_LIMITED` / `RATE_LIMITED`), never bypassed.
- Job postings and portal pages are **untrusted DATA**, never instructions; a
  URL/instruction harvested from posting text can never become a search variant.

Enforced by `tests/test_phase1d_safety.py` and the `atlas doctor`
"portal discovery adapters" check.

## Adapters

Both implement the standard `SourceAdapter` contract and return
`DiscoveryResult`s that the market layer normalizes into append-only
`PortalJobLead`s — **never** an official-verified job.

| Family | Route | Endpoint | Concurrency class |
|---|---|---|---|
| `linkedin` | anonymous public guest | `…/jobs-guest/jobs/api/seeMoreJobPostings/search` (10 cards/page) | `BROWSER_ANONYMOUS` (→ `PORTAL_BROWSER_AUTHENTICATED` only with an explicit `auth_ref`) |
| `naukri` | public search JSON | `…/jobapi/v3/search` (`appid`/`systemid` non-secret client ids) | `BROWSER_ANONYMOUS` |

- Recency (`f_TPR` / `jobAge`), location, and keyword filters are explicit and
  **never silently omitted**. LinkedIn work-type maps to `f_WT`.
- Each card/element is parsed in **isolation** (one malformed card yields a
  finding, not a page crash). A ghost card lacking a stable id + title never
  becomes a canonical lead.
- Indian city aliases (Bengaluru/Bangalore, Hyderabad, …) are normalized;
  LPA/INR salary and experience text are preserved verbatim.

An authenticated LinkedIn/Naukri browser profile is used **only** when an
`auth_ref` is explicitly configured, headless/background, with
authenticated-profile concurrency pinned to **1**. The visible, human-in-the-loop
sign-in command is `atlas portals auth --family <linkedin|naukri> --live` (it
opens a visible Chrome for **you** to sign in; Atlas never types credentials or
solves any check).

## Portal lead vs official observation

- `PortalJobLead` (`atlas/sources/portals/models.py`) — a run-scoped, append-only
  portal observation. `verification_state` starts at `PORTAL_CURRENT_LEAD`.
- `OfficialJobObservation` — the parallel but **separate** evidence event from a
  company career site / ATS. The two never merge identities; a linkage is an
  explicit, evidence-bearing relationship (`portal_official_links`).

Verification (`atlas/market/verification.py`):

- matching official requisition id / apply URL is strongest → `VERIFIED_OFFICIAL`;
- fallback company + title + location is probabilistic and preserves ambiguity
  (multiple candidates → `MANUAL_VERIFICATION`);
- access limitation is **not** closure; closure needs positive official evidence.

## Market campaign / adaptive waves

`atlas/market/` — one deterministic campaign controller (not a second LangGraph
governor, not an LLM):

- append-only, sealed, fingerprinted `MarketCampaign` / `MarketWave`;
- Wave 0 = baseline portal × lane/geography coverage;
- `CoverageDeficitAnalyzer` + `WaveExpansionPlanner` seal a bounded Wave N+1
  (synonyms → geography → alternate source → official follow-up → optional typed
  QueryStrategist, all validated/deduped/capped);
- one central `MarketPoolDispatcher` enforces concurrency classes
  (OFFICIAL_HTTP=4, PUBLIC_BROWSER_ANONYMOUS=2, PORTAL_BROWSER_AUTHENTICATED=1,
  REASONING=2, REPORT_WRITER=1) and a single authenticated-profile owner;
- a run-scoped global budget ledger (`campaign_budget`) is enforced across
  adapters; exhaustion is `PARTIAL_BUDGET`, never `COMPLETE`.

## CLI

```
atlas market plan [--run-id …] [--lanes …] [--geographies …] [--portals …]
atlas market run --live [--run-id …] [--resume]
atlas market status --run-id …
atlas market resume --run-id … --live
atlas market discovered [--run-id …]      # dynamically discovered companies
atlas portals health [--live] [--family linkedin,naukri]
atlas portals auth --family <linkedin|naukri> [--live]   # visible manual sign-in
```

Defaults are dry-run/offline; live network/browser use requires `--live`.
