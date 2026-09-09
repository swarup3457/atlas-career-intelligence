# V1 / V3 Adoption Matrix (Search Hunt Recovery V2)

Reconciles every sanitized, model-visible legacy file (15 V1 + 22 V3 = 37 text
files; 9 nested ZIP containers represented by extracted content; 2 private
candidate-profile Markdown files excluded). Counts reconcile with
`LEGACY_READ_MANIFEST.json` (`model_visible_files: 37`, `excluded_files: 11`,
`actual_total_entries: 48`). Decisions: ADOPT / ADAPT / REJECT / DEFER /
PRIVATE_EXCLUDED.

## V1 (primary behavioral authority)

| File | Decision | Target / reason |
|---|---|---|
| 02_CORE_AGENT_INSTRUCTIONS.md | ADOPT | Six independent lanes, role+tech evidence, official-first, P/R/C/L/U evidence, scoring bands, no-padding, coverage report -> `role_intent_v2.yaml`, qualification.py, matching.py, report.py |
| 03_DAILY_SCHEDULE_PROMPT.md | ADAPT | All-six-lane single run, official sources, truthful tracker; fixed workbook update replaced with immutable per-run output -> graph.py, report.py |
| instruction.md.txt | ADOPT | Concise orchestration wrapper: all lanes, official verification, dedupe, match, tracker honesty -> graph.py |
| 04_BROWSER_AND_LINKEDIN_SETUP.md | ADAPT | Headless/public default, explicit visible login, no auto-apply -> existing browser manager (unchanged) |
| CORE_SCHEDULE_PATCH.md | ADAPT | Single daily run, no evening duplicate, no padding -> cadence policy (existing) |
| New Text Document.txt | ADAPT | Canonical job identity + tracker honesty via SQLite/immutable runs, not one mutable workbook -> models.py identity |
| application-brief-generator SKILL.md | ADOPT | One brief per qualified job; never force a package -> matching.py / report.py Resume_Tailoring |
| application-brief-generator openai.yaml | ADAPT (metadata) | Interface metadata only |
| references/APPLICATION_BRIEF_TEMPLATE.md | ADAPT | Brief structure -> Resume_Tailoring sheet fields |
| international-eligibility-check SKILL.md | ADOPT | Exact-evidence India/remote/sponsorship -> reused `rules.international_eligibility` |
| international-eligibility-check openai.yaml | ADAPT (metadata) | Interface metadata only |
| job-market-trend-analyzer SKILL.md | DEFER | Trend analysis over canonical qualified jobs only; not a search-quality gate |
| job-market-trend-analyzer openai.yaml | DEFER (metadata) | Interface metadata only |
| resume-job-matcher SKILL.md | ADAPT | Evidence labels + lane tailoring, but ONLY after strict qualification -> matching.py runs post-qualification |
| resume-job-matcher openai.yaml | ADAPT (metadata) | Interface metadata only |

## V3 (selective hardening)

| File | Decision | Target / reason |
|---|---|---|
| 00_UPGRADE_STEPS.md | REJECT | Historical workspace checklist, not runtime architecture |
| 01_AGENT_UI_INSTRUCTIONS_V3.md | ADAPT | Freshness, exact coverage, no stale padding; recruiter outreach out of scope |
| 02_CORE_AGENT_INSTRUCTIONS_V3.md | ADAPT | Freshness-before-scoring, duplicate/repost, rolling queue, no early stop; NOT job-count/60-company as a gate -> campaign.py, models.py |
| 03_DAILY_SCHEDULE_PROMPT_V3.md | ADAPT | No-early-stop, company-career coverage; reorder official-before-portals; drop fixed 60 as completion -> campaign.py |
| 04_BROWSER_AND_LINKEDIN_SETUP.md | ADAPT | (identical to V1) |
| 04_HR_OUTREACH_TEMPLATES.md | DEFER | Useful after a qualified application exists; not search-quality recovery |
| 05_COMPANY_SEARCH_UNIVERSE.md | ADAPT | 179-entry expansion pool, provenance-tagged append-only; keep 108 seed baseline -> campaign.py expansion pool |
| 06_TRACKER_SCHEMA_PATCH.md | ADAPT | Company_Coverage / Run_History / repost / freshness fields -> report.py sheets |
| CORE_SCHEDULE_PATCH.md | ADAPT | (identical to V1) |
| instructions.md.txt | REJECT (mixed) | Contains V5 temporary-memory + mutable-workbook mode; salvage ONLY delta/deep queue + never-restart-from-first-company -> campaign.py |
| New Text Document.txt | ADAPT | (identical to V1) |
| application-brief-generator SKILL.md | ADOPT | (identical to V1) |
| application-brief-generator openai.yaml | ADAPT (metadata) | (identical) |
| references/APPLICATION_BRIEF_TEMPLATE.md | ADAPT | (identical) |
| application-response-optimizer SKILL.md | DEFER | Needs real outcome sample; not a pass criterion |
| application-response-optimizer openai.yaml | DEFER (metadata) | Interface metadata only |
| international-eligibility-check SKILL.md | ADOPT | (identical to V1) |
| international-eligibility-check openai.yaml | ADAPT (metadata) | (identical) |
| job-market-trend-analyzer SKILL.md | DEFER | (identical to V1) |
| job-market-trend-analyzer openai.yaml | DEFER (metadata) | (identical) |
| resume-job-matcher SKILL.md | ADAPT | (identical to V1) |
| resume-job-matcher openai.yaml | ADAPT (metadata) | (identical) |

## Private / excluded (never in engineering context, Git, logs, or evidence)

| File | Decision |
|---|---|
| V1/06_CANDIDATE_PROFILE_VERIFIED.md | PRIVATE_EXCLUDED |
| V3/06_CANDIDATE_PROFILE_VERIFIED.md | PRIVATE_EXCLUDED (byte-identical to V1) |
| 9 nested `*.zip` containers | Represented by extracted content; archives not imported as runtime deps |

## Conflict resolution (V1 vs V3)

| Topic | Final decision |
|---|---|
| Cadence | One daily run: due Tier-A delta + resumable 25-40 deep batch |
| Company quantity | Driven by due queue; sealed 30 cohort, extend to 60 only when useful work remains; never a pass gate |
| Job quantity | No minimum/padding; shortlist <= 15; all qualified/raw kept in side data |
| Source order | Official/ATS first; portals disabled in the recovery acceptance |
| Tracker | SQLite + append-only lineage authoritative; unique workbook per run |
| Application packs | Only apply-family jobs; no forced pack |
| Missing skill ZIPs (`company-career-page-sweeper`, etc.) | Archive limitation; map to existing Atlas source/career/coverage modules; do not invent verbatim content |
