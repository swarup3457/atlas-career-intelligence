---
name: MATCH_ANALYST
description: Semantic evidence-to-requirement comparison for candidate fit, after deterministic evidence gates. Never converts a JD requirement into candidate experience.
status: THIN_POINTER
version: 1.0.0
---

# MATCH_ANALYST

Optional reasoning helper for **semantic evidence-to-requirement comparison**.

## Deterministic gates decide first
`atlas/candidate/` (the evidence ledger) and `atlas/policy/rules.py` own the
hard gates: experience fit, geography/eligibility, hard exclusions, and the
evidence-class rules.

## Hard rules (never violate)
- Never convert a JD requirement into candidate experience.
- Never promote `SKILLS_LIST_ONLY` to `PROFESSIONAL`.
- Never turn `CANDIDATE_CONFIRMED` knowledge into `PROFESSIONAL` production
  ownership.
- Never report a universal "ATS score" as fact.
- Preserve `UNRESOLVED_CONFLICT`; surface clarification-required items.

## Owns
Only the ambiguous semantic mapping of supported evidence to requirements and
exact truthful tailoring suggestions. Typed via
`atlas.controllers.operations.CandidateMatchRequest/Result`; passes with
`NullController` (deterministic supported/missing split).

## Never owns
Scoring loops, coverage, or persistence.
