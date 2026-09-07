# Atlas Data Integrity (Phase 0.9)

The `atlas/data_integrity` package turns messy, real-world spreadsheet
exports into **auditable canonical records**. It is deliberately *generic*:
nothing hard-codes sheet positions or column indices. Every sheet and column
is resolved through a configurable mapping layer, so the exact Atlas workbook
schema — or any future schema — is **data, not code**.

The entire subsystem runs fully offline with `controller="none"`
(`NullController`); no LLM and no browser are involved at any point.

## Pipeline stages

```
ingestion   -> typed records (raw + normalized + unknown + provenance)
normalizers -> deterministic, idempotent value canonicalization
identity    -> candidate identity relationships (dup / repost / multi-source / conflict)
validation  -> severity/action findings + quarantine
reconciliation -> idempotent diff into the canonical SQLite store
statuses    -> auditable lifecycle & business-state transitions
report      -> atomic, crash-safe XLSX + JSON quality report
```

Each stage is a separate module, independently testable and deterministic
(no clocks, randomness, locale, or environment reads leak into results).

## Modules

| Module | Responsibility |
| --- | --- |
| `records.py` | `IngestionRecord`, `FieldValue`, `Provenance`, `SheetDiagnostic`, `IngestionResult`. Every cell is kept in **three** forms — `raw`, `normalized`, and (for unmapped columns) `unknown` — plus full provenance (file/sheet/row). |
| `normalizers.py` | Deterministic, **idempotent** normalizers (`whitespace`, `text`, `company`, `url`, `domain`, `date`, `int`, `score`, `bool`, `token`, `header_key`) registered by name in `REGISTRY`. `f(f(x)) == f(x)` for every one. |
| `mapping.py` | `FieldSpec` / `EntitySchema` / `MappingConfig`. Resolves sheet name → entity and raw header → canonical field via alias keys (case/whitespace/punctuation-insensitive). Loadable from a plain dict / YAML. `default_mapping()` expresses the known Atlas schema. |
| `validation.py` | `Severity` (INFO/WARNING/ERROR/CRITICAL) + `Action` (ACCEPT/FLAG/QUARANTINE/REJECT). Generic rules read `FieldSpec` (dtype/domain/required/min/max) so a new field is a mapping change. Worst action wins → `quarantined`/`rejected`. |
| `identity.py` | `IdentityResolver` groups records by the schema's `identity_fields` (with a configurable fallback) and classifies `EXACT_DUPLICATE` / `REPOST` / `MULTI_SOURCE` / `CONFLICT`. Observational fields (dates, freshness, source, live-status) never count as a content conflict. |
| `statuses.py` | Two axes: `RecordStatus` (pipeline lifecycle) and `CanonicalStatus` (business state). Legal transitions are explicit; illegal ones raise `StatusTransitionError`; every applied transition yields an auditable `StatusTransition`. |
| `reconciliation.py` | `Reconciler` folds validated, identity-resolved records into the canonical SQLite store **atomically** (one `StateStore.transaction`) and **idempotently** (content-hash guards + deterministic observation/history IDs). |
| `report.py` | `QualityReport` assembles every stage's output into one deterministic structure and renders a multi-sheet workbook via the generic `ExcelReporter`. |
| `report_writer.py` | Atomic, crash-safe writers (see `REPORT_RELIABILITY.md`). |
| `adversarial.py` | Scenario catalog `A..AU` + expansion generators; never mutates the original fixture; byte/content deterministic. |
| `pipeline.py` | `run_phase09` — the callable end-to-end phase; `run_scenario` / `run_expansion` reused by the tests. |

## Configurable mapping (why it is generic)

A downstream stage never knows that `All_Jobs` is a sheet or that
`Match_Score` is a column. It asks the `MappingConfig`:

```python
schema = mapping.resolve_sheet("All_Jobs")          # -> EntitySchema(entity_type="job")
canonical = mapping.resolve_column(schema, "Match Score")  # -> "match_score"
```

Aliases are matched on a normalized key, so `Company`, `Company Name` and
`company_name` all resolve to `company`. The whole config can be built from
a dict (`MappingConfig.from_dict`) — a different workbook schema is a config
change, not a code change.

## Typed ingestion records (raw + normalized + unknown + provenance)

```python
rec.fields["company"].raw          # exactly what the cell held
rec.fields["company"].normalized   # deterministic canonical form
rec.fields["company"].normalizer   # which normalizer produced it
rec.unknown                        # {header: value} for unmapped columns (never dropped)
rec.provenance                     # source_file / sheet_name / row_index / ...
```

Nothing is silently lost: unmapped columns are preserved as `unknown`
provenance and surfaced as `UNKNOWN_COLUMN` (INFO) findings. On the real
fixture, `Run_Summary`'s many un-modelled counters are captured this way.

## Candidate identity relationships

The identity key is the normalized token of the schema's `identity_fields`
(e.g. `company + job_id` for a job), with a fallback (`company + role +
location`) when the primary key is empty. Clusters are classified so
reconciliation knows how to treat them:

- **EXACT_DUPLICATE** — identical stable payload.
- **REPOST** — same identity observed with later timestamps.
- **MULTI_SOURCE** — same identity discovered via different sources.
- **CONFLICT** — same identity with contradictory *content* values.

The real fixture contains a genuine same-id / different-role conflict, which
the resolver surfaces automatically.

## Validation, findings & quarantine

Findings describe a problem and prescribe an action; they never mutate data.
A record's disposition is the **most severe** action across its findings.
`QUARANTINE`/`REJECT` records are separated and **never** reach the canonical
store. On the clean real fixture nothing is quarantined (0 ERROR/CRITICAL);
messy URL/enum columns are surfaced as `WARNING`/`FLAG` so they are visible
without destroying data.

## Reconciliation & the canonical store (StateStore v3)

Migration **v3** adds (additively, without breaking v1/v2):

- `canonical_jobs` — one row per identity; content-hash guarded.
- `job_observations` — every sighting of a canonical job (multi-source, repost).
- `status_history` — every auditable status change.
- `quarantine` — records held back from canonicalization.
- `data_integrity_runs`, `applied_operations` — run bookkeeping + idempotency keys.

Reconciliation is **idempotent**: re-running the same input creates nothing
new (`created=0, updated=0, observations_added=0, status_changes=0`). Within a
batch, one deterministic *primary* record per identity drives canonical
content while the rest contribute observations only — so conflicting
duplicates cannot flip-flop. Closure is authoritative within a batch: if any
member of an identity cluster is observed `CLOSED` (e.g. a `Closed_or_Rejected`
row contradicting an active `All_Jobs` row), the canonical job is closed and
the transition is recorded.

## Auditable statuses & transitions

`RecordStatus`: `INGESTED → NORMALIZED → VALIDATED → RECONCILED → CANONICAL`
(with `QUARANTINED`/`REJECTED` reachable from any non-terminal state).
`CanonicalStatus`: `UNKNOWN / ACTIVE / CLOSED / SUPERSEDED / QUARANTINED`,
including legal `ACTIVE→CLOSED` (closure) and `CLOSED→ACTIVE` (repost).
`apply_transition` validates legality and emits an audit record; illegal
transitions raise `StatusTransitionError`.

## Running it

```python
from atlas.data_integrity.pipeline import run_phase09
result = run_phase09(
    "fixtures/real/Atlas_Jobs_2026-08-13.xlsx",
    "output/phase09",
)
assert result.fixture_unchanged        # the immutable fixture is never mutated
assert result.all_scenarios_passed     # A..AU
```

See `REPORT_RELIABILITY.md` for the atomic write protocol, `PHASE09_AUDIT.md`
for the fixture audit, and `PHASE09_TEST_MATRIX.md` for the full test map.
