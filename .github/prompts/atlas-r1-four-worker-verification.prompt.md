---
description: Run the Atlas R1.1 four-worker verification bridge in a new Agent chat.
agent: atlas-r1-verification-coordinator
---

Use `atlas-r1-verification-coordinator` in this NEW Agent chat. Verify both custom
agents are loaded and that the coordinator has only `runSubagent` and
`atlas-runtime/*`, while the verifier has native Browser and `atlas-runtime/*`.
Default parent discovery run: `r1-prod-20260911_171517`.
Default selection manifest:
`C:\Atlas-Copilot-Runs\ATLAS_PRODUCTION_R1_20260911_171517\verification_selection_manifest.json`.
An explicitly supplied parent run or manifest path overrides these defaults.

Normalize and seal the real manifest without modifying it. Reuse the manifest; do
not discover new leads, call Freehire, or browse from the root. The coordinator must create four durable
`VERIFY_JOB_LEAD_BATCH` tasks, dispatch the exact
`atlas-job-lead-verifier-vscode` worker once per available batch in one parallel
fan-out wave, and fail closed if that agent or its Browser/MCP tools are not
loaded.

Require one terminal classification per assigned lead, lead checkpoints,
inline typed results, COMMIT_ACK, Python validation, at most one focused
correction per incomplete batch, a fan-in barrier, and a checkpoint workbook
after two terminal batches. Then build the final run workbook and cumulative
master workbook from latest VALID committed outcomes. Do not allow root-sequential
Browser fallback. Stop after the four-batch proof and report durable statuses,
commit IDs, validation actions, and any exact missing obligations.