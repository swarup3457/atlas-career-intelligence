# Legacy Table Authority

Canonical code: `atlas/persistence/sqlite.py`. Some early tables overlap with the
Phase 1A/1B canonical model. Production code uses ONLY the canonical tables; the
old per-run tables are retained (non-destructively) as legacy/read-only history.
Phase 1B.1 does NOT destructively migrate.

## Authoritative vs legacy

| Concern | Authoritative (canonical) | Legacy (read-only) |
|---|---|---|
| Company identity | `company_registry` (+ `company_aliases`) | `companies` (per-run) |
| Company ↔ source link | `company_source_relationships` | — |
| Source instances | `source_instances` | `sources` |
| Jobs | `canonical_jobs` + `raw_discovery_observations` + `job_observations` | `jobs`, `job_sources` |
| Coverage | `coverage_records`, `coverage_plans`, `coverage_attempts` | — |
| Company checks | discovery observations / health history | `company_checks` |

## Enforcement

- `ProductionSearchRuntime` writes only the canonical tables (plan/coverage/
  attempts/raw observations/canonical jobs/source instances). A regression test
  (`tests/test_legacy_table_authority.py`) runs a full fixture run and asserts
  the legacy tables (`companies`, `sources`, `jobs`, `job_sources`,
  `company_checks`) remain EMPTY while the canonical model is populated.
- Facade/repository access: `CompanyRegistry` (`company_registry`),
  `StateStore` canonical methods (`upsert_source_instance`, `upsert_coverage*`,
  `stage_raw_observation`, `upsert_canonical_job`, `add_observation`). New
  production code must use these, never the legacy tables.

## Migration posture

Additive only. Schema migrations are append-only and each new version adds
tables/columns without editing shipped migrations (latest: v7
`raw_discovery_observations`). A destructive migration would require a prior file
backup (see `docs/STATE_MODEL.md`) and is out of scope for this phase.
