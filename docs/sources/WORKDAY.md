# Workday public careers (CXS) adapter — highest risk

Module: `atlas/sources/ats/workday.py` · family `workday` · type `ATS_WORKDAY`

Workday publishes **no official public job-board API**. Each employer self-hosts
a careers site on a Workday tenant whose own front-end calls an internal JSON
endpoint under `/wday/cxs/`. Atlas treats this as **observed public web
behavior, not a stability promise**, and uses only the same public interface the
employer's own careers page uses.

## Endpoints

- Careers URL: `https://{tenant}.{dc}.myworkdayjobs.com/{locale}/{site}`. The
  datacenter shard (`wd1`/`wd3`/`wd5`/`wd103`/…) is part of the hostname and
  **cannot be guessed** — it must come from the real careers URL. A missing
  tenant/datacenter/site is a `CONFIG_ERROR`.
- Search: **POST** `.../wday/cxs/{tenant}/{site}/jobs` with
  `{"appliedFacets":{}, "limit":N, "offset":M, "searchText":""}` and the same
  `Origin`/`Referer`/`Content-Type` the careers page sends (honest, never
  spoofed/rotated). This is the ONE allowed POST — a facet/keyword search
  carrying no candidate data.
- Detail: **GET** `.../wday/cxs/{tenant}/{site}{externalPath}` →
  `{jobPostingInfo:{jobDescription, title, location, jobReqId, …}}` (the list
  has no descriptions).

## Pagination

Hard page cap **20** (`limit > 20` → HTTP 400), so the adapter caps `limit` at
20 and pages via `offset`/`total`. One page per `search()` call; `has_more`/
`next_cursor` expose further pages for the planner.

## Date semantics

`postedOn` is a **relative** display string ("Posted Today", "Posted 30+ Days
Ago"). It is recorded as **`RELATIVE_POSTED_TEXT`** provenance with the raw
string in `provenance.posted_raw`; `posted_at` stays `None`. An absolute posted
date is never fabricated from relative text.

## Identity / schema variation

`source_job_id` prefers `jobReqId`, then `bulletFields[0]` (which may be a
string or a `{label,value}` object), then `externalPath`/title.
`canonical_url` = the public job URL (`.../{locale}/{site}{externalPath}`).

## Health / failures — and the no-bypass posture

`health_check` POSTs `limit:1` with the same headers and expects
`jobPostings`/`total`. A response missing `jobPostings` → `EXTRACTION_UNRESOLVED`
(schema drift, **never a false zero**). An HTML/login/challenge page
masquerading as JSON is detected → `ANTI_BOT`/`LOGIN_WALL` (`ACCESS_LIMITED`) and
Atlas **stops** — it never solves a challenge, rotates IPs/proxies/user-agents,
or spoofs to evade. 429/5xx/timeout mapped as usual.

## Canary result (2026-09-08)

`base_url: https://workday.wd5.myworkdayjobs.com/en-US/Workday` returned live
jobs (incl. IND.Pune) with a HEALTHY probe — Workday **PASSES**. If a future
tenant returns a challenge, the adapter reports `ACCESS_LIMITED` and the Workday
component is truthfully PARTIAL for that board — never a fabricated PASS.
