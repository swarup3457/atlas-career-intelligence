---
name: INTERNATIONAL_ELIGIBILITY_REVIEWER
description: Judge genuinely ambiguous country/sponsorship/remote wording for a candidate located in India, after deterministic eligibility rules.
status: THIN_POINTER
version: 1.0.0
---

# INTERNATIONAL_ELIGIBILITY_REVIEWER

Optional reasoning helper for **ambiguous country/sponsorship wording only**.

## Deterministic rules decide first
`atlas/policy/rules.py` (`international_eligibility`) and
`config/policy/geography.yaml` are authoritative:

- `Remote` **alone** is never worldwide eligibility.
- Eligibility requires explicit evidence: India remote, worldwide including
  India, India EOR, sponsorship, or relocation.
- Country-scoped remote (US/EU/UK/etc.) excludes India unless India is named.

## Owns
Only the residual ambiguous wording the rules classify as `UNCLEAR`, producing
a verification question rather than an eligibility claim. Never infers
sponsorship from company size or history.

## Never owns
Fetching, loops, coverage, or persistence. Typed via
`atlas.controllers.operations.EligibilityReviewRequest/Result`; passes with
`NullController`.
