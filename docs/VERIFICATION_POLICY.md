# Verification Policy

Canonical code: `atlas/policy/rules.py`, `atlas/policy/status.py`
(see also `docs/STATUS_MODEL.md`). All gates are deterministic; the optional
`VERIFICATION_REVIEWER` controller is bounded by the deterministic ceiling.

## Official verification (P0-9)

`VERIFIED_OFFICIAL` when ALL of:

- a specific, employer-controlled role page (`page_kind == specific_role_page`);
- company/title/location/requisition identity sufficiently aligned;
- current role content is visible;
- no positive closure evidence.

**Final application submission is never required.** An Apply control is
**corroboration**, not a mandatory criterion.
`OFFICIAL_PAGE_FOUND_APPLY_PATH_UNCONFIRMED` is used ONLY when currentness is not
directly visible AND the route cannot be confirmed — i.e. genuine uncertainty
about role currentness/identity — not merely because a button cannot be clicked.

## Freshness (P0-10)

- known employer posted date → age band (`0-7 days` … `STALE`);
- confirmed live official page, reliable date absent → `LIVE_DATE_UNKNOWN`;
- no reliable date and no confirmed live page → `DATE_UNKNOWN` (never "live");
- future/contradictory date → `DATA_CONFLICT` (manual review);
- crawl/cache/first_seen/index dates NEVER substitute for the employer date.

## Closure

Positive explicit closure evidence (a closed banner, "no longer accepting", a
reliably-parsed expired employer deadline) DOMINATES access-state metadata.
Login/CAPTCHA/access limitation ALONE never closes; a block combined with an
explicit closed banner is still CLOSED with both observations retained
(`classify_closure`). CLOSED is lifecycle only, not a verification level.

## Experience

Mandatory minimum, maximum/up-to, and preferred/desirable are extracted
SEPARATELY (explicit separator alternation, not a loose character class). Only a
hard mandatory minimum drives automatic rejection; a preferred/max-only figure
never establishes a minimum. Title alone never decides.

## International eligibility

Generic sponsorship wording that is explicitly negated ("no visa sponsorship")
does NOT grant eligibility. A named India location survives a general
no-sponsorship clause. Country-scoped remote excludes India unless India is
named. "Remote" alone remains `UNCLEAR`.

## Status migration safety (P1-5/6/7/8)

- `Verified Authorized Recruiter` → `MANUAL_VERIFICATION` (NOT VERIFIED_OFFICIAL).
- `PORTAL_ONLY_UNVERIFIED` → `MANUAL_VERIFICATION` (not a current lead).
- `Live — Date Unknown` is a freshness concept and is NOT a verification alias.
- `CLOSED` is lifecycle only; the user-facing display derives it from lifecycle
  (`combined_verification_display` / `recommendation_display`).
- The generic source ladder is `SourceEvidenceLevel`; `VerificationLevel`
  remains a backward-compatible alias to disambiguate it from the business
  verification level.

## Controller ceilings (P0-19)

An LLM verification suggestion can never exceed the deterministic source-evidence
ceiling, cannot override a hard deterministic exclusion/experience/location veto,
and must satisfy operation-specific schemas (allowed lanes/levels/classes,
confidence in `[0,1]`, evidence subsets). Invalid output yields
`INVALID_FELL_BACK` plus a persisted audit finding.
