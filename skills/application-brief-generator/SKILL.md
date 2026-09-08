---
name: application-brief-generator
description: Methodology for a decision-ready application brief. Deterministic brief schema and rendering live in Python (atlas.controllers.operations.ApplicationBriefRequest/Result and atlas.reporting); this skill supplies only the rubric.
status: THIN_METHODOLOGY
version: 1.0.0
---

# application-brief-generator

Reasoning/presentation methodology only. The deterministic brief schema and
rendering are owned by Python (`atlas.controllers.operations` +
`atlas.reporting`); this skill never owns loops, state, or persistence.

## Rules
- One brief per canonical job. Use the official application URL when available.
- Separate facts from inference; never omit missing hard requirements.
- Never mark an application submitted without candidate confirmation.

## Brief contents
Rank/priority; company/title/job id; location/work mode; posted + first-seen
dates; experience requirement; official apply URL + discovery source;
verification level + evidence; international eligibility evidence; match score
and breakdown; hard requirements met/missing; why it fits; rejection risks;
exact truthful resume changes; candidate facts requiring confirmation;
interview focus; recommended next action.

Use `references/APPLICATION_BRIEF_TEMPLATE.md`, populated from typed data — not
from free-form state.
