# VS Code Worker Foundation Reuse Matrix

| pattern | source repository/file | license | REUSE/ADAPT/REJECT | Atlas destination | reason |
|---|---|---|---|---|---|
| thin pointer / canonical workflow | `MadsLorentzen/ai-job-search/AGENTS.md` | MIT | ADAPT | `.github/skills/atlas-job-hunt` | Keep business policy and schemas in Atlas YAML/Python; agent files point inward. |
| state-first search and seen continuity | `.claude/skills/job-scraper/SKILL.md` | MIT | ADAPT | `atlas/vscode_hunt` task ledger | Load pending work/history before search; Python owns durable state. |
| bounded profile-driven query categories | `.claude/skills/job-scraper/search-queries.md` | MIT | ADAPT | task package lane obligations | Use India Java/.NET/React/enterprise lanes, not Danish portal assumptions. |
| full-detail hydration and stale/closed checks | `.claude/skills/job-scraper/SKILL.md` | MIT | ADAPT | worker contract and validation | Cards are not evidence; canonical details and source health are required. |
| pure stable job key and dedup | `tools/job_key.py` | MIT | ADAPT | Atlas dedup/history validators | Stable keys prevent repeated postings across runs. |
| rank after search/dedup | `.claude/commands/rank.md` | MIT | ADAPT | future candidate ranking phase | Keep expensive fit ranking separate from discovery and hard gates. |
| portal skill discovery contract | `.claude/commands/add-portal.md` | MIT | DEFER | future source adapters | Useful extensibility pattern; not needed for this local worker proof. |
| typed/Pydantic evidence models | `interviewstreet/hiring-agent/models.py` | MIT | ADAPT | worker result validation | Structured fields and evidence strings improve deterministic validation. |
| PDF extraction and provenance/cache | `interviewstreet/hiring-agent/pdf.py`, `github.py` | MIT | DEFER | candidate evidence phase | Candidate evidence only; no candidate files or browser search imported here. |
| role-specific explainable scoring | `interviewstreet/hiring-agent/roles.py`, `score.py` | MIT | DEFER | later match-analysis phase | Keep scoring after verified search results. |
| auto-apply, credentials, CAPTCHA bypass | `wellfound_autoApply` | verify upstream before copying | REJECT | none | Violates Atlas read-only safety contract. |
