# Live Canary Operations

The Phase 1C-A canaries are **opt-in, read-only, low-volume** checks of the four
official ATS adapters. Nothing here runs by default; instances are disabled
unless `--live` is passed, and `atlas doctor` stays fully offline.

## Two levels

### 1. Per-family adapter canary
```
atlas adapters list
atlas adapters canary --all                       # DRY-RUN (no network)
atlas adapters canary --family lever --live       # one board, one probe + one search
atlas adapters canary --config <path> --all --live
```
Reports, per board: identity, request status (`OK`/`ZERO`/`ERROR:<CATEGORY>`),
result count, pagination (`more`), health state, any limitation, and up to five
job samples (title/location/id only — **never** full descriptions).

### 2. Production canary (full graph, parallel dispatcher)
```
atlas production-canary run    --config <path> --live
atlas production-canary resume --run-id <id> --live
atlas production-canary status --run-id <id>      # offline inspection
```
Runs the SAME LangGraph production graph with the bounded parallel dispatcher
over the canary companies (one `CANARY` lane per board), reaching a truthful
`COMPLETE`/`PARTIAL`. A synthetic candidate is used (no PII required).

## Configuration

`config/canary/ats_canary.example.yaml` documents the schema.
`config/canary/ats_canary.live.yaml` holds the verified-live boards. Per family:

| Family | Identity field(s) |
|---|---|
| greenhouse | `board_token` |
| lever | `site` (+ `eu: true` for EU) |
| ashby | `board_name` (case-sensitive) |
| workday | `base_url` (full careers URL — datacenter shard cannot be guessed), or `tenant` + `datacenter` + `site` |

Global `limits`: `max_companies` (≤12), `max_jobs_per_company` (≤100),
`workers`. Per-board `request_budget` bounds HTTP calls.

## Safety rules

- Read-only GET (plus the public Workday CXS search POST the careers page
  issues). No apply, no login/account, no credentials, no CAPTCHA/anti-bot
  bypass, no proxy/IP/UA rotation, no fetching links inside posting text.
- A blocked board → `ACCESS_LIMITED`/`SOURCE_UNAVAILABLE`, recorded truthfully;
  the run continues other boards and never bypasses.
- Do not commit full live job descriptions; evidence samples are capped at five
  jobs per family (title/location/id).

## Verified-live result (2026-09-08)

| Family | Board | Status |
|---|---|---|
| greenhouse | gitlab | OK (Bangalore roles) |
| lever | leverdemo | OK |
| ashby | Ashby | OK |
| workday | workday (wd5) | OK (IND.Pune roles) |

`production-canary run` over all four reached **COMPLETE** with 4/4 children
terminal and one valid Excel report.

## Real-web tests

`tests/test_ats_real_web_canary.py` is marked `real_web` and is **deselected by
default**. Run explicitly with:
```
pytest -m real_web tests/test_ats_real_web_canary.py
```
