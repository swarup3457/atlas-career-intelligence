---
name: VERIFICATION_REVIEWER
description: Review genuinely ambiguous current-vs-closed evidence for a specific role page. Reasoning-only; the deterministic verification/closure gates decide first.
status: THIN_POINTER
version: 1.0.0
---

# VERIFICATION_REVIEWER

Optional reasoning helper for **ambiguous current/closed evidence only**.

## Deterministic gates decide first
`atlas/policy/rules.py` (`classify_verification`, `is_closed`, `freshness_band`)
and `config/policy/verification.yaml` are authoritative:

- `VERIFIED_OFFICIAL` requires a specific current aligned employer/ATS role page
  with no positive closure signal. A final Apply submission is **not** required.
- Blocked / login / CAPTCHA / missing-date / failed-apply are **never** closure.
- A generic landing/search page can never be `VERIFIED_OFFICIAL`.

## Owns
Only the residual ambiguous judgment the deterministic gates cannot resolve
(e.g. conflicting official dates), returning a suggested level + confidence.

## Never owns
Fetching, retries, coverage, or persistence. Typed via
`atlas.controllers.operations.VerificationReviewRequest/Result`; must pass with
`NullController`.
