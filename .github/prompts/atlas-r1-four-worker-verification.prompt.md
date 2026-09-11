---
description: Run the Atlas R1.1 four-worker verification bridge in a new Agent chat.
agent: atlas-r1-verification-coordinator
---

Use `atlas-r1-verification-coordinator` for this task. Start a fresh verification
run linked to the explicitly supplied R1 selection manifest and parent discovery
run. Reuse the manifest; do not discover new leads, call Freehire, or browse from
the root. The coordinator must create four durable
`VERIFY_JOB_LEAD_BATCH` tasks, dispatch the exact
`atlas-job-lead-verifier-vscode` worker once per available batch in one parallel
fan-out wave, and fail closed if that agent or its Browser/MCP tools are not
loaded.

Require one terminal classification per assigned lead, lead checkpoints,
inline typed results, COMMIT_ACK, Python validation, at most one focused
correction per incomplete batch, a fan-in barrier, checkpoint workbook, final
run workbook, and cumulative master workbook. Do not allow root-sequential
Browser fallback. Stop after verification proof and report durable statuses,
commit IDs, validation actions, and any exact missing obligations.