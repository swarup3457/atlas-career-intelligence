---
name: international-eligibility-check
description: Methodology for judging ambiguous international/remote eligibility for a candidate in India. Deterministic country/sponsorship rules run first in atlas.policy.rules; this skill handles only residual ambiguity.
status: THIN_METHODOLOGY
version: 1.0.0
---

# international-eligibility-check

Deterministic rules decide first (`atlas.policy.rules.international_eligibility`
+ `config/policy/geography.yaml`). This skill handles only residual ambiguous
wording and never owns loops/state/persistence.

## Deterministic first pass
- `Remote` alone is NOT worldwide eligibility.
- Eligible only with explicit evidence: India remote, worldwide including India,
  India EOR, sponsorship, or relocation.
- `EU/US/UK remote` or country-specific payroll means not eligible unless India
  is explicitly included or an EOR/relocation route is stated.

## Skill (ambiguous cases only)
For text the rules classify `UNCLEAR`, produce a verification question rather
than an eligibility claim. Never infer sponsorship from company size or history.

## Output
Eligibility status, exact supporting wording, unresolved issue, recommended
action, and a recruiter question when needed.
