# Status Model

Status: **implemented (Phase 1B).** Legacy Workspace documents overloaded a
single "status" across discovery, lifecycle, verification, recommendation,
source health, coverage, and task outcomes. Phase 1B keeps these as **distinct
typed axes** (`atlas/policy/status.py`, public record in
`config/policy/status_aliases.yaml`).

## Separated axes

- **`DiscoveryStatus`** — pipeline position: `DISCOVERY_CANDIDATE`, `NORMALIZED`,
  `DEDUPLICATED`, `VERIFICATION_PENDING`, `VERIFIED`, `MATCHED`, `REPORTED`,
  `REJECTED`.
- **`JobLifecycleStatus`** — is the vacancy open? `ACTIVE`, `CLOSED`, `UNKNOWN`.
- **`VerificationLevel`** — strength of current evidence: `VERIFIED_OFFICIAL`,
  `OFFICIAL_PAGE_FOUND_APPLY_PATH_UNCONFIRMED`, `PORTAL_CURRENT_LEAD`,
  `MANUAL_VERIFICATION`, `SUSPICIOUS_REJECTED`.
- **`RecommendationStatus`** — the candidate action: `PRIORITY_APPLY`,
  `STRONG_APPLY`, `APPLY_AFTER_TAILORING`, `STRETCH`, `MANUAL_REVIEW`,
  `MONITOR`, `REJECT`, `CLOSED`.
- **`SourceHealthState`**, **`CoverageStatus`**, **`TaskStatus`** — re-exported
  from their owning modules for a single import point.

## Key rules

- **`CLOSED` is lifecycle truth** — never a verification or source-health
  shortcut. A closed role is a lifecycle fact, not a verification level.
- **Legacy aliases map to canonical states.** Spacing/case variants (e.g.
  "Verified Official", "Portal-Only Unverified", "No Longer Accepting") resolve
  to a canonical value **without creating a duplicate** canonical state
  (`normalize_verification`, `normalize_lifecycle`, `normalize_recommendation`).
- **A combined human-facing column may derive `CLOSED`** from lifecycle for
  display (`combined_verification_display`), while the internal axes stay
  separate.

## Verification, freshness & closure

Deterministic rules live in `atlas/policy/rules.py`, configured by
`config/policy/verification.yaml`.

- **`VERIFIED_OFFICIAL`** requires a *specific, current, identity-aligned*
  official/ATS role page with **no positive closure** signal. A final Apply
  submission is **not** required (`require_final_apply_submission: false`). A
  generic landing or search-results page can never be `VERIFIED_OFFICIAL`.
- **Blocked/login/CAPTCHA/missing-date/failed-apply are never closure.** Closure
  requires positive evidence (e.g. "position filled", "applications closed");
  the non-closure conditions explicitly cannot close a role (`is_closed`).
- **Freshness bands** (`freshness_band`): `0–7`, `8–14`, `15–30`,
  `31–45 (exceptional)`, else `STALE`; a live official page with no reliable
  date is `LIVE_DATE_UNKNOWN`.
- **Never use crawl, cache, or `first_seen` dates as the posting date** — those
  sources are explicitly forbidden as the employer posting date.

See [PRODUCTION_SEARCH_ARCHITECTURE.md](PRODUCTION_SEARCH_ARCHITECTURE.md) for
`CoverageStatus`/`SourceHealthState` details and
[SEARCH_POLICY.md](SEARCH_POLICY.md) for the geography/experience gates.
