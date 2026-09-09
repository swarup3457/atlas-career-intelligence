# Search Hunt Architecture V2 (Recovery)

Committed before load-bearing code. Reconciles sanitized V1/V3, the current
six-lane / 108-company policy, the failed-workbook counterexamples, and the
separation of collection, prefilter, hydration, qualification, match, display,
and reporting.

## Layering (separate expensive network work from cheap local logic)

```
BoardSnapshot (immutable, network)         atlas/hunt/models.py
  -> LanePrefilterDecision (local recall)  atlas/hunt/prefilter.py
    -> JobDetailRevision (hydrate once)    atlas/hunt/models.py
      -> RoleQualificationDecision (strict) atlas/hunt/qualification.py
        -> CandidateMatchDecision           atlas/hunt/matching.py
          -> diversified shortlist          atlas/hunt/matching.py
            -> unique workbook + artifacts   atlas/hunt/report.py
              -> quality validator           atlas/hunt/validator.py
```

The root LangGraph governor (`atlas/hunt/graph.py`) owns completion; prose never
does. Stages: `LOAD_PROFILE -> LOAD_POLICY -> PLAN_DUE_COMPANIES -> SEAL_BATCH ->
DISCOVER_OR_REUSE_SOURCE -> COLLECT_SNAPSHOTS [Send] -> PREFILTER_SIX_LANES ->
HYDRATE_UNIQUE_JOBS [Send] -> QUALIFY_ROLES [Send] -> ANALYZE_COVERAGE
(-> NEXT_BATCH) -> MATCH_CANDIDATE -> DIVERSIFY_SHORTLIST -> BUILD_REPORT ->
VALIDATE_OUTPUT -> COMPLETE | COMPLETE_NO_MATCHES | PARTIAL_BUDGET |
WAITING_FOR_NETWORK | WAITING_FOR_HUMAN | FAILED`.

## Reused Atlas infrastructure (do not rebuild)

- Deterministic gates: `atlas/policy/rules.py` — `extract_experience`,
  `experience_eligible`, `international_eligibility`, `evaluate_exclusions`,
  `freshness_band`, `classify_closure`, `classify_verification`.
- Policy loader/models, SQLite persistence, source adapters (`atlas/sources`),
  browser manager, backup/restore, run-lock, checkpoints.
- Atomic reopen-validated workbook discipline from
  `atlas/reporting/mapping.write_report` (mirrored by the hunt writer).

## New qualification contract (the fix)

`config/policy/role_intent_v2.yaml` defines, per lane: `allowed_role_families`,
`excluded_role_families`, `required_anchor_groups` (conjunction of groups,
`any_of` within a group), `support_signals` (corroboration only),
`wrong_stack_signals`, `reject_when_wrong_stack_dominant`, and for the fallback
`require_transferable_or_neutral` + `transferable_or_neutral_any`.

`RoleQualificationDecision.status` in
`{QUALIFIED, REJECT_WRONG_STACK, REJECT_ROLE_FAMILY, REJECT_EXPERIENCE,
REJECT_LOCATION, REJECT_FRESHNESS, NEEDS_DETAIL, AMBIGUOUS_REVIEW}` stores
matched anchor groups, dominant stack, role family, exclusion family,
experience interpretation, evidence-revision id, and the role-policy hash.

Qualification order (deterministic, first veto wins): role-family exclusion ->
allowed-family gate -> required anchor groups -> dominant wrong-stack ->
(fallback) transferable/neutral -> experience -> geography -> closure/freshness.

### Explicitly rejected defaults

- The `classify_lane` ANY-positive behaviour (any positive title OR any tech
  term) is rejected as the product default.
- The two-lane collapse is rejected; all six lanes are obligations.
- A mutable single workbook as sole truth is rejected; SQLite + immutable
  per-run artifacts are authoritative.
- The forced review-package fallback is removed.

## Immutable board snapshots, requalify without network

`BoardSnapshot` is immutable (company/source/run identity, adapter/parser
versions, pages, content hash, retrieved_at, raw jobs). A role-policy change
creates a child qualification run that requalifies stored `JobDetailRevision`
evidence with **zero** network calls
(`atlas hunt requalify --collection-run-id <ID> --new-run-id <ID>`). A report
defect rebuilds only from stored decisions
(`atlas hunt rebuild-report ...`).

## Company plan and cohorts

Stable baseline = the current structured 108-company seed
(`config/policy/company_seed.yaml`, 5 groups). The V3 179-entry universe is a
provenance-tagged append-only expansion pool, never a wholesale replacement.
The acceptance seals a deterministic **stratified 30-company** cohort across the
five groups before the first request; immutable **15-company** extension batches
(each a new sealed-batch hash) continue up to 60 when no match and due work
remains. A failed company is never swapped for an easier one under the same run.

## Unique, non-overwriting output

`output/production/runs/<RUN_ID>/Atlas_Jobs_<YYYYMMDD-HHMMSS>_<RUN_ID>.xlsx`
plus `run_manifest.json`, `sealed_company_plan.json`,
`sealed_batch_history.jsonl`, `query_plan.json`, `company_coverage.json`,
`source_coverage.json`, `raw_observations.jsonl`,
`qualification_decisions.jsonl`, `recommendations.json`, `run_lineage.json`. One
line is appended to `output/production/run_index.jsonl`. No earlier workbook is
overwritten; `Atlas_Jobs_LATEST.xlsx` is not touched during recovery.

## Worker topology (bounded)

Source discovery 4; official HTTP board fetch 4-6; generic browser 1-2;
profile-bound browser exactly 1; prefilter 6-8 CPU tasks; hydration 4-6;
qualification 6-8 CPU tasks; ambiguous reasoning <= 2; report writer exactly 1.
A board snapshot is owned by one `(campaign, company, source)` task; lane
evaluators consume it without new network calls.

## Workbook sheets

`All_Jobs` (strictly qualified only), `Company_Coverage` (one row per company x
lane obligation), `Source_Coverage` (one row per source attempt), `Closed_or_
Rejected` (reason codes incl. wrong-stack controls), `Run_Summary` (all six
lanes, batches, remaining queue, relevant count, truthful outcome), and
`Resume_Tailoring` (may be empty). Empty `Company_Coverage`/`Source_Coverage`
is always a validation failure.
