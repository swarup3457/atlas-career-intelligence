# Agent / Skill Migration Plan

## Purpose

An existing ChatGPT Workspace Atlas Agent already contains the real
business specification: production instructions, search lanes, company
universe, verification rules, candidate profile, ATS rules, cadence,
deduplication rules, Excel schema, skills, and examples of good/bad runs.
This foundation build creates the *infrastructure* to host that content
locally, systematically, without inventing replacement business logic
today.

## What exists today (infrastructure only)

- **Loader**: `atlas/agents_loader.py` — `load_agents(agents_dir)` /
  `load_skills(skills_dir)` parse YAML-frontmatter + Markdown-body spec
  files (`agents/*.agent.md`, `skills/<name>/SKILL.md`).
- **Validation**: `parse_spec_file()` enforces a minimal required schema
  (`name`, `description` required; `status` must be one of `PROVEN`,
  `IMPLEMENTED`, `SCAFFOLDED`, `NOT_YET_BUILT`) and raises
  `SpecValidationError` with a clear message otherwise.
- **Placeholder agent files** (metadata only, no business rules):
  `agents/ORCHESTRATOR.agent.md`, `COMPANY_DISCOVERY.agent.md`,
  `CAREER_SEARCH.agent.md`, `ATS_SEARCH.agent.md`,
  `PORTAL_SEARCH.agent.md`, `VERIFICATION.agent.md`,
  `DEDUPLICATION.agent.md`, `CANDIDATE_MATCH.agent.md`,
  `REPORTING.agent.md`.
- **Placeholder skill**: `skills/career-page-check/SKILL.md`, documenting
  the one mechanical capability that IS proven today
  (`atlas/workers/career_page.py`).
- **Test coverage**: `tests/test_foundation_suite.py` section B loads and
  validates every agent/skill file, and asserts that malformed specs
  (missing required fields) are correctly rejected.

## Migration steps once the Workspace Agent files are provided

1. **Inventory** each Workspace Agent file's role and map it to one of the
   existing placeholder agent files above (or propose a new one if none
   fits — do not force-fit).
2. **Split reasoning from mechanics.** Anything that is genuinely
   deterministic bookkeeping (retry counts, completion tracking,
   deduplication *mechanism*) belongs in `atlas/orchestration/` or
   `atlas/persistence/sqlite.py`, not in an agent's Markdown body — only
   the *rules* (e.g. "these fields make two jobs duplicates") belong in
   the agent/skill spec.
3. **Fill in the placeholder `.agent.md` bodies** with the real
   instructions, updating `status:` from `NOT_YET_BUILT`/`SCAFFOLDED` to
   `IMPLEMENTED` only once matching code exists and is tested, and to
   `PROVEN` only once it has been exercised against real systems.
4. **Add concrete source adapters** under `atlas/sources/ats/` and
   `atlas/sources/portals/` implementing `atlas.sources.base.BaseSource`,
   one per ATS/portal named in the Workspace Agent's source coverage
   rules — NOT built in this foundation pass.
5. **Wire the Excel schema** into `atlas/reporting/excel.py` once the
   Workspace Agent's Excel schema is known — today's `ExcelReporter` only
   exposes a generic `write_generic_sheet()` helper.
6. **Re-run `tests/test_foundation_suite.py`** after each migration step
   to ensure the loader/validation layer still accepts every updated spec
   file, and add new business-rule-specific tests alongside.

## Explicit non-goal for this build

Sections of the Workspace Agent describing company universe, cadence,
verification rules, candidate profile, and Excel schema are NOT
transcribed anywhere in this repository yet. Nothing in `agents/*.agent.md`
today should be treated as authoritative business logic — it is
structure only.
