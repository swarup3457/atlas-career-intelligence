# Phase 0.9 Audit

Exact, reproducible audit of the Phase 0.9 data-integrity run against the
immutable fixture. The machine-readable equivalent of this document is written
by the pipeline to `output/phase09/PHASE09_AUDIT.json` (and `_run_summary.json`).

## Fixture integrity (immutable)

| Property | Value |
| --- | --- |
| Path | `fixtures/real/Atlas_Jobs_2026-08-13.xlsx` |
| SHA-256 (expected) | `440B9587610FBA5B8C0E61498FE52EE92A5139749F1F29D57ED7734A2A25B8AC` |
| SHA-256 (before run) | `440b9587610fba5b8c0e61498fe52ee92a5139749f1f29d57ed7734a2a25b8ac` |
| SHA-256 (after run) | `440b9587610fba5b8c0e61498fe52ee92a5139749f1f29d57ed7734a2a25b8ac` |
| Bytes | 59,720 |
| Mutated by the pipeline? | **No** — `fixture_unchanged = True` |

The pipeline computes the hash before and after every stage (including
adversarial generation and the 20,000-row expansion) and asserts they are
identical. All adversarial artifacts are written to `output/phase09/` — never
back to the fixture.

## Sheet schema (audited from the workbook, not hard-coded)

| Sheet | Entity | Header cols | Data rows | Records ingested |
| --- | --- | --- | --- | --- |
| All_Jobs | job | 23 | 25 | 25 |
| New_Companies | company | 11 | 9 | 9 |
| Company_Coverage | company_coverage | 16 | 16 | 16 |
| Source_Coverage | source_coverage | 12 | 22 | 22 |
| Closed_or_Rejected | job | 11 | 1 | 1 |
| Resume_Tailoring | resume_tailoring | 12 | 8 | 8 |
| Recruiter_Contacts | recruiter_contact | 13 | 0 | 0 |
| Run_Summary | run_summary | 28 | 1 | 1 |
| **Total** | | | | **82** |

All eight sheets resolve to a known entity via the configurable mapping.
`Run_Summary` intentionally models 13 fields and preserves the remaining
columns as **unknown** provenance (surfaced as `UNKNOWN_COLUMN` findings) —
nothing is dropped.

## Main pipeline results (real fixture)

| Metric | Value |
| --- | --- |
| Records ingested | 82 |
| Findings (total) | 43 |
| Findings by severity | INFO 16, WARNING 27, ERROR 0, CRITICAL 0 |
| Quarantined | 0 |
| Identity clusters | 81 |
| Relationships | SINGLETON 80, CONFLICT 1 (others 0) |
| Canonical jobs | 25 |
| Observations | 26 |
| Reconciliation | created 25, updated 0, unchanged 1, **closed 1**, reposted 0 |
| Status changes | 25 |

The single CONFLICT is genuine data present in the fixture (one job appears
twice with the same identity but a different role). The single closed job comes
from the `Closed_or_Rejected` sheet. Re-running reconciliation is idempotent
(created 0, updated 0, observations_added 0, status_changes 0).

## Adversarial + performance

- Scenario catalog: **A..AU (47 codes)**, all pass (44 dedicated workbooks +
  3 expansion sizes represented via the performance section + 1 combined-chaos).
- Expansion (deterministic, seeded), reference timings:
  - 500 rows → ~0.8 s (~625 rows/s)
  - 5,000 rows → ~7.4 s (~672 rows/s)
  - 20,000 rows → ~29.6 s (~676 rows/s)
- The original fixture hash is re-verified after all generation: unchanged.

## Deliverables (written atomically, crash-safe)

| Artifact | Path |
| --- | --- |
| Data Quality Report (XLSX) | `output/phase09/Atlas_PHASE09_Data_Quality_Report.xlsx` |
| Data Quality Report (JSON) | `output/phase09/Atlas_PHASE09_Data_Quality_Report.json` |
| Audit (JSON) | `output/phase09/PHASE09_AUDIT.json` |
| Run summary (JSON) | `output/phase09/_run_summary.json` |
| Canonical store (SQLite v3) | `output/phase09/phase09_state.sqlite` |
| Adversarial copies (A..AU) + expansions | `fixtures/generated/` |

The XLSX report has a stable multi-sheet schema: `Summary`,
`Sheet_Diagnostics`, `Findings`, `Quarantine`, `Identity_Relationships`,
`Reconciliation`, `Adversarial_Scenarios`, `Performance`.

## Reproduce

```python
from atlas.data_integrity.pipeline import run_phase09
result = run_phase09(
    "fixtures/real/Atlas_Jobs_2026-08-13.xlsx",
    "output/phase09",
)
assert result.fixture_unchanged
assert result.all_scenarios_passed
```

Numbers above are from the reference run recorded in
`output/phase09/_run_summary.json`; per-stage wall-clock timings vary by
machine but the counts and hashes are deterministic.
