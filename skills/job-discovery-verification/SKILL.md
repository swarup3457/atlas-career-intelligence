---
name: job-discovery-verification
description: Verification methodology for a discovered role. Source execution, pagination, coverage, and retries are owned by adapters/planner/governor; this skill supplies only the official-first verification rubric.
status: THIN_METHODOLOGY
version: 1.0.0
---

# job-discovery-verification

Verification methodology only. Discovery execution, source health, pagination,
coverage, and retries are owned by `atlas/sources`, `atlas/planning`, and
`atlas/orchestration` — never by this skill. Deterministic verification gates
live in `atlas.policy.rules.classify_verification`.

## Official-first rubric
- Most effort goes to official employers and employer-controlled ATS.
- A specific current aligned official/ATS role page with no positive closure is
  `VERIFIED_OFFICIAL`; a final Apply submission is not required.
- A generic employer search/landing page is discovery evidence, not
  `VERIFIED_OFFICIAL`.
- Portal-only credible current leads are `PORTAL_CURRENT_LEAD`; do not let them
  dominate the priority queue when official roles exist.
- Blocked/login/CAPTCHA/missing-date are never closure.

## Output
Suggested verification level + evidence for genuinely ambiguous cases; the
deterministic gates decide the rest.
