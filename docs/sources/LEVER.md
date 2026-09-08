# Lever Postings API adapter

Module: `atlas/sources/ats/lever.py` · family `lever` · type `ATS_LEVER`

Read-only against the public **Postings API**. The application POST endpoint is
never called.

## Endpoints

- Base: `https://api.lever.co/v0/postings/{site}` (EU residency:
  `https://api.eu.lever.co/v0/postings/{site}`).
- List: `GET /v0/postings/{site}?mode=json&skip=X&limit=Y` — returns a
  **top-level JSON array**. `mode=json` forces JSON regardless of `Accept`.
  Documented `skip`/`limit` pagination is honored (`PAGINATION` capability).
- Detail: `GET /v0/postings/{site}/{posting_id}?mode=json`.

## Identity

`site` from `instance.metadata["site"]` / `instance.site`, or parsed from a
`jobs.lever.co/{site}` URL (EU host → EU API base). `source_job_id` = the
posting UUID; `canonical_url` = `hostedUrl`.

## Fields / date semantics

- `text` → title; `categories.{location,department,team,commitment}`;
  `workplaceType` is **top-level** (`remote`/`hybrid`/`on-site`) → work mode.
- `createdAt` (epoch-ms, when present) → `posted_at` (ISO) with provenance
  **`EMPLOYER_POSTED_AT`**; absent → provenance **`UNKNOWN`** (never fabricated).
  The README documents no date field, so this is treated defensively.

## Pagination

`page`/`limit` → `skip=(page-1)*limit`, `limit`. `has_more` when a full page is
returned; `next_cursor` = the next `skip`. Only published postings are exposed
(`is_active = ACTIVE`).

## Health / failures

Unknown site → 404 → `SOURCE_UNAVAILABLE`; missing posting on detail → 404 →
`INVALID_RESPONSE`; 429/5xx/timeout mapped; non-array response →
`EXTRACTION_UNRESOLVED`; malformed items isolated.

## Canary

Verified-live official example: `site: leverdemo` — see
`config/canary/ats_canary.live.yaml`.
