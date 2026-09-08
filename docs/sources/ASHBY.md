# Ashby Public Job Postings API adapter

Module: `atlas/sources/ats/ashby.py` · family `ashby` · type `ATS_ASHBY`

Read-only against the single public **Job Postings API** endpoint. No
application action is ever performed.

## Endpoint

- `GET https://api.ashbyhq.com/posting-api/job-board/{jobBoardName}?includeCompensation={true|false}`
- Returns `{apiVersion, jobs:[...]}` with **all** currently published postings in
  one response. **No pagination**, and **no separate detail endpoint** — each
  posting already carries `descriptionHtml`/`descriptionPlain`, so the adapter
  declares no `DETAIL` capability and populates the description from the list.

## Identity

`board_name` from `instance.metadata["board_name"]`, or parsed from a
`jobs.ashbyhq.com/{jobBoardName}` URL (case-sensitive). The docs do not
guarantee a per-job `id`, so `source_job_id` is derived from `id` when present,
else the final `jobUrl` path segment, else a deterministic hash of
title + `publishedAt`. `canonical_url` = `jobUrl`.

## Fields / date semantics

- `title`, `location`, `department`, `team`, `employmentType`, `workplaceType`
  (`OnSite`/`Remote`/`Hybrid`) / `isRemote` → work mode.
- `publishedAt` (ISO, "last published") → `posted_at` with provenance
  **`EMPLOYER_POSTED_AT`** (may move on re-publish); there is no employer-updated
  timestamp, so none is invented.
- `isListed: false` postings are direct-link-only and are excluded from the
  public listing. Listed postings are `is_active = ACTIVE`.

## Health / failures

`health_check` expects `jobs`/`apiVersion`. Unknown board → 404 →
`SOURCE_UNAVAILABLE`; 429/5xx/timeout mapped; response missing `jobs` →
`EXTRACTION_UNRESOLVED`; malformed items isolated; sparse objects handled
defensively.

## Canary

Verified-live official example: `board_name: Ashby` — see
`config/canary/ats_canary.live.yaml`.
