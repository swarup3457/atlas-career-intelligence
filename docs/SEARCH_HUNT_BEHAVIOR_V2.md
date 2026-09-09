# Search Hunt Behavior V2 (Recovery)

Status: architecture lock. Authority order: this prompt + user clarification >
sanitized V1 files > selective V3 hardening > current Atlas infrastructure >
external references. This document is committed **before** load-bearing code.

## Why this recovery exists

The previous Phase 2A official-first acceptance proved transport and
persistence but failed the search objective: 20 visible jobs (15 from one
company), no explicit Java/Spring/React titles, Ruby-on-Rails and Go admitted
into Java/backend output, a product-manager role admitted via the word `API`,
an SDET admitted into development output, zero apply-quality recommendations,
empty coverage tabs, and a forced review package.

Root cause: `atlas/candidate/eligibility.classify_lane` classifies a job into a
lane if **any** positive title **or any** technology term appears. Because
`JAVA_BACKEND.technology_terms` contains `API`, `REST`, `SQL`, and
`microservices`, and `positive_titles` contains the bare title `Backend
Engineer`, a Ruby/Go/PM/SDET posting that merely mentions those words is
mislabeled Java. This is an ANY-positive bug, not a transport bug.

## Restored behavior (from V1)

Company by company -> open/discover the official career source -> execute
explicit role/technology/location/experience search intent -> inspect full job
evidence -> retain only genuinely qualified target roles -> record every
company/lane outcome -> continue while sealed work remains -> publish one new
immutable workbook.

## Non-negotiable behavioral rules

1. **Six independent lanes** — JAVA_BACKEND, JAVA_FULLSTACK, REACT_FRONTEND,
   DOTNET, ENTERPRISE_HR_PAYROLL_INTEGRATION, GENERAL_SOFTWARE. A production
   all-lane run never silently reduces to two lanes.
2. **Conjunctive qualification** — a job qualifies for a lane only when a
   **development role family** AND the lane's **required technology anchor
   group(s)** are both present in hydrated evidence. Support signals (`API`,
   `REST`, `SQL`, `microservices`) are corroboration, never sufficient alone.
3. **Wrong-stack rejection** — a dominant Ruby/Go/Rust/PHP/Node-only/Python-only
   stack with no Java anchor is `REJECT_WRONG_STACK`, and it does not re-enter
   `GENERAL_SOFTWARE` merely because it is software engineering.
4. **Role-family exclusion** — QA/SDET, product/program management,
   DevOps/SRE, support-only, functional-consulting, sales/BPO, data/ML, and
   incompatible-seniority roles are excluded from the candidate shortlist.
5. **Collect once, classify many** — one immutable board snapshot feeds all six
   local lane prefilters; six lanes never trigger six identical network fetches.
6. **Hydrate before qualifying** — a title-only lead is `NEEDS_DETAIL`, never a
   strong match.
7. **Coverage determines completion** — finding three jobs is not completion
   while sealed company x lane obligations remain. Completing every obligation
   with no suitable role is a truthful `COMPLETE_NO_MATCHES`, not a licence to
   admit Ruby/Go/QA/PM/senior roles.
8. **No padding, no forced package** — packages are built only for apply-family
   recommendations. Zero suitable jobs -> zero packages.
9. **Immutable history, unique workbook** — every run gets a new id + lineage
   and writes `Atlas_Jobs_<YYYYMMDD-HHMMSS>_<RUN_ID>.xlsx`; no earlier workbook,
   run, snapshot, checkpoint, or evidence bundle is overwritten or deleted.
10. **Official-first, portals off** — the Search Recovery V2 acceptance is
    official-source-only. No LinkedIn/Naukri/Foundit/Indeed/Wellfound.

## Experience policy (user's latest instruction wins)

Eligible bands: `0-2, 0-3, 1-2, 1-3, 2, 2+, 2-3, 3`, and suitable `3+`. Exact
mandatory minimum <= 3 is normally eligible; `3+` may qualify when overall
evidence is strong; hard `4+` fails unless the private candidate profile
satisfies it. Preferred/desirable years are not hard minimums; `up to N` is a
maximum only; a title never invents years; an explicit `2-3` requirement
overrides a misleading senior title after responsibility review.

## Terminal states

`ENGINEERING_PASS_WITH_MATCHES` (>=1 relevant official live job),
`ENGINEERING_PASS_COMPLETE_NO_MATCHES` (full coverage, zero suitable jobs, no
padding), `PARTIAL_BUDGET`, `WAITING_FOR_NETWORK`, `WAITING_FOR_HUMAN`,
`FAILED`. Only `ENGINEERING_PASS_WITH_MATCHES` is a successful job harvest.
