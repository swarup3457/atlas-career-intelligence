# Greenhouse Job Board API adapter

Module: `atlas/sources/ats/greenhouse.py` · family `greenhouse` · type `ATS_GREENHOUSE`

Read-only against the public **Job Board API**. The application submission
endpoint (a Basic-Auth POST) is never called.

## Endpoints

- Base: `https://boards-api.greenhouse.io/v1`
- List: `GET /v1/boards/{board_token}/jobs` — returns **all** posts in one
  response with `meta.total`. **Not paginated** (so no `PAGINATION` capability).
- Detail: `GET /v1/boards/{board_token}/jobs/{id}` — adds `first_published`,
  `application_deadline`, and the HTML `content`.

## Identity

`board_token` from `instance.metadata["board_token"]`, else the company id /
tenant, else parsed from a `boards.greenhouse.io/{token}` /
`job-boards.greenhouse.io/{token}` URL. A missing token is a `CONFIG_ERROR`.
`source_job_id` = the job post `id`; `canonical_url` = `absolute_url`.

## Date semantics

- List exposes only `updated_at` → `updated_at` with provenance
  **`EMPLOYER_UPDATED_AT`**; `posted_at` stays `None`.
- Detail exposes `first_published` → `posted_at` with provenance
  **`EMPLOYER_POSTED_AT`**, plus the sanitized description.

## Filtering / active state

A post whose `internal_job_id` is null is a prospect / general-interest post and
is excluded. The board lists only live posts, so `is_active = ACTIVE`.

## Health / failures

`health_check` probes the jobs list and expects `jobs`/`meta`. Unknown board →
404 → `SOURCE_UNAVAILABLE`; unknown job on detail → 404 → `INVALID_RESPONSE`;
429 → `HTTP_429` (with `Retry-After`); 5xx → `HTTP_5XX`; timeout → `TIMEOUT`; a
malformed item is isolated (finding); a response missing `jobs` →
`EXTRACTION_UNRESOLVED` (never a false zero).

## No-apply boundary

Only the two GET endpoints above are ever called. Description text is untrusted;
scripts are stripped and secrets redacted; links inside it are never fetched.

## Canary

Verified-live example: `board_token: gitlab` (lists Bangalore roles) — see
`config/canary/ats_canary.live.yaml`.
