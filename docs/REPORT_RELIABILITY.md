# Atlas Report Reliability (Phase 0.9)

Phase 0.9 artifacts (the Data Quality Report `.xlsx` + `.json`, and the audit
`.json`) are written by `atlas/data_integrity/report_writer.py` using a single
**atomic, crash-safe** protocol. A reader must only ever observe a *complete*
file — never a half-written one — and a run must never lose its output because
a file happens to be open in Excel.

## The write protocol

For every artifact:

1. **temp** — serialize to a sibling temp file `<stem>.part<suffix>` (e.g.
   `report.part.xlsx`) in the **same directory**, so the final promotion is an
   atomic `os.replace` on the same filesystem. The real suffix is preserved so
   the temp is a valid, reopenable file; the `.part` marker makes it
   unmistakably incomplete and discoverable for recovery.
2. **reopen / validate** — reopen the temp and confirm it is structurally
   sound: a real xlsx zip containing the expected sheets, or parseable JSON. A
   corrupt temp is deleted and `ReportWriteError` is raised — it can **never**
   become the final file.
3. **os.replace** — atomically swap the validated temp into place. Readers see
   either the old complete file or the new complete file, never a mix.

The JSON writer additionally `flush()` + `os.fsync()`s the temp before
promotion.

## Locked final → deterministic alternate policy

On Windows a final path can be locked because the workbook is open in Excel;
`os.replace` then raises `PermissionError` (or an `OSError` sharing violation,
winerror 32/33). Instead of losing the output, the writer falls back to a
**deterministic** alternate path:

```
report.xlsx        (locked)
report.locked.xlsx     <- first fallback
report.locked-2.xlsx   <- next, if that is also locked
...
```

The alternate is deterministic (same lock ⇒ same alternate path), so an
operator always knows where to find the output. `WriteResult.locked` and
`WriteResult.used_alternate` report what happened. Only a genuinely
unexpected `OSError` (not a sharing violation) cleans up the temp and
re-raises as `ReportWriteError`.

## Incomplete-temp crash recovery

A crash between steps 1 and 3 leaves a `*.part.*` temp behind. Two safeguards:

- Every atomic write first calls `recover_incomplete_temps(dir, final_name)`
  to sweep a stale temp for the artifact it is about to write (reported in
  `WriteResult.recovered`).
- `recover_incomplete_temps(dir)` (no `final_name`) sweeps **all** `*.part.*`
  temps in a directory — call it on startup to guarantee no stale temp is ever
  mistaken for real output. A still-locked temp is left in place (best effort)
  rather than crashing recovery.

Because a temp only becomes final *after* it validates, a recovered temp is by
definition incomplete and is safe to delete.

## Deterministic report content

Generated workbooks pin their core document properties (`created`/`modified`/
`creator`) to a fixed timestamp, so identical input yields identical logical
content run-to-run (openpyxl otherwise stamps `datetime.now()`). Adversarial
copies are additionally re-packed with fixed zip member timestamps
(`save_workbook_deterministic`) and validated for content-determinism via
`workbook_content_digest`.

## What this guarantees

- **Atomicity** — no partial/corrupt final file is ever observable.
- **Validation-before-promote** — a structurally broken artifact is discarded,
  not shipped.
- **No data loss under lock** — a locked destination diverts to a predictable
  alternate path.
- **Crash tolerance** — leftover temps are recoverable and self-identifying.
- **Reproducibility** — identical inputs produce identical report content.

## Verification

See `tests/test_phase09_report_writer.py`:

| Test | Property |
| --- | --- |
| `test_atomic_workbook_write_creates_valid_file` | temp → validate → replace; no leftover temp |
| `test_atomic_json_write_roundtrips` | JSON atomic write + reopen |
| `test_validation_failure_discards_temp_and_raises` | bad temp never becomes final |
| `test_json_validation_failure_on_unserializable` | unserializable payload raises, no file written |
| `test_locked_final_uses_deterministic_alternate` | locked → `report.locked.xlsx`, deterministic |
| `test_recover_incomplete_temp_files` | crash-temp sweep removes `*.part.*` |
| `test_write_recovers_stale_temp_first` | write auto-recovers a stale temp |
| `test_recover_targets_single_artifact` | targeted recovery leaves other temps untouched |

The end-to-end pipeline test (`test_phase09_pipeline.py`) additionally asserts
each artifact's `WriteResult` is `validated=True, locked=False` and that no
`*.part.*` temp remains in the output directory.
