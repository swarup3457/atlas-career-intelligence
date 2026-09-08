---
name: resume-job-matcher
description: Semantic tailoring methodology on top of deterministic evidence gates. Evidence extraction/scoring constraints live in Python (atlas.candidate + atlas.policy.rules); this skill supplies only ambiguous semantic fit and exact truthful tailoring.
status: THIN_METHODOLOGY
version: 1.0.0
---

# resume-job-matcher

Deterministic evidence gates decide first (`atlas.candidate` ledger +
`atlas.policy.rules`). This skill supplies only semantic fit judgment and exact
truthful tailoring; it never owns loops/state/persistence.

## Evidence classes (never blur)
- `PROFESSIONAL`, `PROJECT_PRODUCT`, `CANDIDATE_CONFIRMED`, `SKILLS_LIST_ONLY`,
  `UNSUPPORTED`, and preserved `UNRESOLVED_CONFLICT`.
- Never equate project/confirmed/listed/unsupported with professional evidence.
- Never convert a JD requirement into candidate experience.

## Method
1. Extract mandatory vs preferred requirements.
2. Build an evidence matrix from the ledger.
3. Identify the correct lane and transferable evidence.
4. Split missing keywords into: supported wording that can be added,
   clarification-required, and genuine gaps.
5. Produce exact truthful edits only.

## Output
Supported/missing/clarification-required requirements, lane fit, and exact
resume edits — never a fabricated universal ATS score.
