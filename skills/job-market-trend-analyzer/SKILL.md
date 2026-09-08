---
name: job-market-trend-analyzer
description: Interpret deterministic market aggregates across the six lanes into narrative insights. Consumes computed aggregates only — never reads a workbook and never re-counts.
status: THIN_METHODOLOGY
version: 1.0.0
---

# job-market-trend-analyzer

Interpretation only. Deterministic analytics are owned by Python; this skill
never reads an Excel workbook (Excel is report-only, never an input) and never
owns loops/state/persistence.

> Note: the legacy version hardcoded a specific master tracker workbook as an
> input. That workbook dependency is removed — inputs are computed aggregates.
> (The exact legacy filename is recorded in `atlas/imports/migration.py`.)

## Inputs (already computed)
Canonical verified-job counts aggregated by search lane, role family, location,
work mode, experience band, mandatory skills, employer/industry, discovery
source, and verification status — plus candidate evidence coverage by class.

## Rules
- Count canonical verified jobs only; interpret, do not re-aggregate.
- Flag search bias (e.g. one lane exceeding a threshold) vs true market signal.
- Identify a gap only when repeated across multiple realistic roles.
- Do not invent salary averages from incomplete data.

## Output
Period + sample size, lane distribution, top roles/locations, recurring skills
by lane, candidate evidence coverage, genuine gaps, a study plan, and search
strategy adjustments.
