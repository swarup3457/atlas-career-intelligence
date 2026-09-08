---
name: recruiter-outreach-prep
description: BLOCKED_PENDING_POLICY. This workflow requires an authoritative public recruiter-contact policy that has not been supplied. It must not run until that policy is approved.
status: BLOCKED_PENDING_POLICY
version: 1.0.0
---

# recruiter-outreach-prep — BLOCKED

This skill is intentionally **blocked**.

## Why
The legacy version required `05_PUBLIC_RECRUITER_CONTACT_POLICY.md`, which was
**not present** in the Workspace import package. Atlas must not invent a missing
authority for handling recruiter contact data (a privacy-sensitive area).

## Status
`BLOCKED_PENDING_POLICY`. No recruiter research, contact discovery, drafting, or
outreach is performed until an authoritative public recruiter-contact policy is
supplied and approved. See `atlas/imports/migration.py` and
`docs/LEGACY_MIGRATION_MATRIX.md`.

## When unblocked (future, not now)
Any future implementation must: use public professional evidence only; never
guess names/emails/hiring managers; reject accommodation/privacy/legal/support/
sales/vendor/data-broker contacts; record source, confidence, safe-to-contact
status, and restrictions; prepare drafts only and never send automatically.
