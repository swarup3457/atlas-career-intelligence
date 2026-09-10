# AI Job Search Reuse Verification

| source file | Atlas destination | implemented | tested | notes |
|---|---|---:|---:|---|
| `.agents/skills/freehire-search/SKILL.md` | `atlas/discovery/freehire.py` | yes | yes | Public keyless API, full descriptions, bounded facets, failure isolation. |
| `.agents/skills/freehire-search/url-reference.md` | `atlas/discovery/freehire.py` | yes | yes | `/facets` and `/agent/jobs/search` response contract adapted. |
| `.claude/skills/job-scraper/SKILL.md` | `atlas/discovery/service.py`, `prefilter.py` | yes | yes | State-first flow, source health, detail-first acceptance, deferred queue. |
| `.claude/skills/job-scraper/search-queries.md` | bounded query matrix | yes | yes | Function-oriented Java/React/.NET/HR integration families. |
| `tools/job_key.py` | `atlas/discovery/models.py` | yes | yes | Canonical URL, provider ID, then normalized fallback identity. |
| `tools/rank_state.py` | `atlas/discovery/report.py` | adapted | yes | Compact state/report projection; ranking remains downstream of hydration. |
| `AGENTS.md` | `.github/agents/atlas-company-discovery-vscode.agent.md` | yes | yes | Thin-pointer source-of-truth and read-only safety. |

No new provider was added during hardening. The real Freehire provider was already
operationally used by the previous run; this pass fixes reporting, identity, audit,
and validation boundaries only.
