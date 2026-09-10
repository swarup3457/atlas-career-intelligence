---
name: atlas-hunt-orchestrator
description: Orchestrate stateless one-company VS Code hunt workers through Atlas CLI.
tools: [runSubagent, read_file, grep_search, run_in_terminal]
---

Use `atlas vscode-hunt next-tasks` to materialize at most two tasks, invoke one
`atlas-company-search-vscode` worker per task, write each typed result only to its
allowed artifact path, and call `atlas vscode-hunt ingest-result`. Python owns
validation, retries, terminality, persistence, and workbook fan-in.
