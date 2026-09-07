# Phase 0.9 Test Matrix

All Phase 0.9 tests are **fully offline** (`NullController`, no browser, no
network) and touch only `tmp_path` or the read-only fixture. They are marked
`unit`, `integration`, and (for the large expansions) `slow`; none are
`real_web`, so they all run in the default suite.

Fixture: `fixtures/real/Atlas_Jobs_2026-08-13.xlsx`
SHA-256 `440B9587610FBA5B8C0E61498FE52EE92A5139749F1F29D57ED7734A2A25B8AC`,
59,720 bytes (immutable — never mutated by any test).

## Test files → required behaviors

| Test file | Required behavior covered |
| --- | --- |
| `test_phase09_data_integrity.py` | Normalizer determinism + idempotency; configurable mapping/alias resolution; fixture ingestion (raw+normalized+unknown+provenance); validation finding model; identity relationships; status transitions; NullController is an offline no-op. |
| `test_phase09_reconciliation.py` | **Import idempotency**; canonical store roundtrip; **multi-source**; **repost**; **status closed reporting**; **cross-sheet contradiction closure**; **migration safety** (v1/v2/v3 intact, no re-run on reopen); **transaction rollback** + atomic commit + idempotency-key guard. |
| `test_phase09_report_writer.py` | **Atomic report** write/validate; **locked** deterministic-alternate policy; **incomplete-temp crash recovery**; JSON writer; validation-before-promote. |
| `test_phase09_adversarial.py` | Catalog completeness (A..AU); **never mutates original**; **deterministic mutations** (content digest); **malformed workbooks** (non-xlsx bytes, truncated zip, missing file); every non-performance scenario detected. |
| `test_phase09_pipeline.py` | Callable pipeline creates final XLSX+JSON+audit outputs; fixture unchanged; real per-stage timings; atomic write results; all scenarios pass. |
| `test_phase09_performance.py` | **500 / 5,000 / 20,000** expansion ingest with real measured timings + throughput; monotonic scaling; never mutates original. |

## Required-behavior checklist (from the Phase 0.9 brief)

| Required behavior | Where verified |
| --- | --- |
| import idempotency | `test_reconcile_fixture_is_idempotent` |
| multi-source | `test_multi_source_relationship` |
| repost | `test_repost_relationship` |
| status closed reporting | `test_closed_status_reporting`, `test_cross_sheet_contradiction_closes_job` |
| atomic report / validation / locked / deferred policy | `test_phase09_report_writer.py` (8 tests) |
| crash temp | `test_recover_incomplete_temp_files`, `test_write_recovers_stale_temp_first` |
| transaction rollback | `test_transaction_rolls_back_on_error`, `test_transaction_commits_atomically` |
| migration safety | `test_migration_v3_present_and_v1_v2_intact`, `test_reopen_does_not_rerun_migrations`, plus the pre-existing `tests/test_state_store.py` |
| malformed workbooks | `test_ingest_non_xlsx_bytes_returns_load_error`, `test_ingest_truncated_zip_returns_load_error`, `test_ingest_missing_file_returns_load_error` |
| reconciliation | `test_phase09_reconciliation.py` (all) |
| deterministic mutations | `test_generation_is_content_deterministic`, `test_expansion_is_content_deterministic`, `test_generation_never_mutates_original` |
| 500/5000/20000 performance | `test_expansion_ingests_all_rows_with_timing[500|5000|20000]`, `test_expansion_scaling_is_monotonic` |
| callable pipeline creating final outputs + timings | `test_run_phase09_creates_outputs_and_preserves_fixture` |

## Adversarial scenario catalog (A..AU, 47 codes)

`represented=yes` means the case is validated via explicit result diagnostics
(the three expansion sizes and the combined-chaos case) rather than a single
dedicated pass/fail workbook — as permitted by the brief. All 47 pass.

| Code | Name | Category | Expected signals | Represented |
| --- | --- | --- | --- | --- |
| A | Exact duplicate row | identity | rel:EXACT_DUPLICATE; code:DUPLICATE_IDENTITY | no |
| B | Case-variant company | identity | code:DUPLICATE_IDENTITY | no |
| C | Whitespace-padded identity | identity | code:DUPLICATE_IDENTITY | no |
| D | Diacritic-variant company | identity | code:DUPLICATE_IDENTITY | no |
| E | Whitespace/tab in Job_ID | identity | code:DUPLICATE_IDENTITY | no |
| F | Empty Job_ID | identity | records==1; load_ok | no |
| G | Missing required column | schema | code:REQUIRED_MISSING; quar>=1 | no |
| H | Extra unknown column | schema | unknown_col; code:UNKNOWN_COLUMN | no |
| I | Reordered columns | schema | records==1 | no |
| J | Renamed header alias | schema | records==1; load_ok | no |
| K | Repost (new dates) | identity | rel:REPOST | no |
| L | Multi-source | identity | rel:MULTI_SOURCE | no |
| M | Closed status | status | rec_closed>=1 | no |
| N | Conflicting role | identity | rel:CONFLICT | no |
| O | Non-numeric score | value | code:NON_NUMERIC | no |
| P | Out-of-range score | value | code:OUT_OF_RANGE_HIGH; quar>=1 | no |
| Q | Malformed URL | value | code:MALFORMED_URL | no |
| R | Date format variants | value | records==2; load_ok | no |
| S | Future-dated posting | value | records==1; load_ok | no |
| T | Numeric stored as text | value | records==1; load_ok | no |
| U | Boolean-ish variants | value | records==2; load_ok | no |
| V | Embedded newlines | encoding | records==1; load_ok | no |
| W | Very long cell | encoding | records==1; load_ok | no |
| X | Emoji / non-ASCII | encoding | records==1; load_ok | no |
| Y | Duplicate headers | schema | dup_header | no |
| Z | Empty sheet | schema | records==0; load_ok | no |
| AA | Blank rows interspersed | schema | records==2; blank>=1 | no |
| AB | Blank header gap | schema | code:UNKNOWN_COLUMN | no |
| AC | Company case alias | identity | code:DUPLICATE_IDENTITY | no |
| AD | Location variant | identity | rel:CONFLICT | no |
| AE | Work-mode variant | identity | rel:CONFLICT | no |
| AF | Experience variant | identity | rel:CONFLICT | no |
| AG | Unknown verification enum | value | code:UNKNOWN_ENUM_VALUE | no |
| AH | Unknown live-status enum | value | code:UNKNOWN_ENUM_VALUE | no |
| AI | Cross-sheet contradiction | status | rec_closed>=1 | no |
| AJ | Orphan closed job | status | rec_closed>=1 | no |
| AK | Company missing coverage | cross | load_ok | yes |
| AL | Duplicate company coverage | cross | code:DUPLICATE_IDENTITY | no |
| AM | Negative count | value | code:OUT_OF_RANGE_LOW; quar>=1 | no |
| AN | Non-integer count | value | code:NON_INTEGER | no |
| AO | Leading-zero id | value | records==1; load_ok | no |
| AP | SQL-injection text | injection | records==1; load_ok | no |
| AQ | Formula injection | injection | code:FORMULA_INJECTION; quar>=1 | no |
| AR | Expansion 500 | performance | records==500 | yes |
| AS | Expansion 5000 | performance | records==5000 | yes |
| AT | Expansion 20000 | performance | records==20000 | yes |
| AU | Combined chaos | combined | quar>=1; code:DUPLICATE_IDENTITY | yes |

### Signal vocabulary

- `code:X` — finding code `X` present.
- `rel:X` — identity relationship `X` present in some cluster.
- `quar>=N` — at least `N` records quarantined/rejected.
- `rec_closed>=N` — reconciliation closed at least `N` canonical jobs.
- `records==N` / `records>=N` — ingested record count.
- `unknown_col` / `dup_header` / `blank>=N` / `load_ok` — sheet-diagnostic signals.

Notes on faithful representation:
- **AQ / AU injection**: a leading `=` is stored by Excel/openpyxl as a
  *formula* and reads back as `None` (no calc engine), which would defang the
  test. The `@` prefix is an equally real OWASP CSV/DDE-injection trigger that
  survives as literal text, so the validator genuinely quarantines it.

## Measured performance (reference run)

Ingest of deterministic expansions (single-writer, offline; timings vary by
machine, but scaling is ~linear at a stable throughput):

| Rows | File size | Ingest time | Throughput |
| --- | --- | --- | --- |
| 500 | ~52 KB | ~0.8 s | ~625 rows/s |
| 5,000 | ~465 KB | ~7.4 s | ~672 rows/s |
| 20,000 | ~1.84 MB | ~29.6 s | ~676 rows/s |
