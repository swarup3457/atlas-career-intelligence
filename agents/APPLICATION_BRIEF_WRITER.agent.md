---
name: APPLICATION_BRIEF_WRITER
description: Render a user-facing application brief from typed application-brief data. Presentation only; never fabricates evidence and never marks an application submitted.
status: THIN_POINTER
version: 1.0.0
---

# APPLICATION_BRIEF_WRITER

Optional reasoning helper that **renders a user-facing application brief** from
already-computed, typed data.

## Owns
Turning typed brief inputs (supported points, missing hard requirements,
rejection risks, exact truthful tailoring, confirm-before-applying items) into
readable prose using `skills/application-brief-generator`'s template.

## Hard rules
- Separate facts from inference; never omit missing hard requirements.
- Never invent technologies, versions, metrics, scale, dates, ownership,
  certifications, compensation, authorization, or production experience.
- Never mark an application submitted without candidate confirmation.

## Never owns
Verification, matching, coverage, or persistence — those are decided upstream.
Typed via `atlas.controllers.operations.ApplicationBriefRequest/Result`; passes
with `NullController`.
