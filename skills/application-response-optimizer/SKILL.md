---
name: application-response-optimizer
description: Interpret outcome aggregates (recruiter replies, assessments, interviews) into next-run priority suggestions. Deterministic aggregation is owned by Python; this skill only interprets computed segments.
status: THIN_METHODOLOGY
version: 1.0.0
---

# application-response-optimizer

Interpretation only. Deterministic outcome aggregation and weighting are owned
by Python (`atlas` analytics); this skill never re-counts, never reads a
workbook, and never owns loops/state/persistence.

## Inputs (already computed)
Tracker-confirmed outcomes aggregated by source, company, role lane, match
band, freshness, resume version, and outreach-used.

## Rules
- Do not conclude a source is bad from a single rejection.
- Require a meaningful sample before strong conclusions; label sparse insights
  preliminary.
- Prefer sources/companies/lanes/freshness bands with better confirmed outcomes.

## Output
Strongest/weakest signals, next-run weighting suggestions, and resume/outreach
experiments to test — as narrative, not as state mutations.
