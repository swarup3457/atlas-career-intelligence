"""Atlas durable local SQLite state layer.

This is the authoritative, high-frequency durable store for Atlas runs.
LangGraph checkpoints live in a SEPARATE SQLite database (see
atlas/orchestration/checkpoints.py) managed by langgraph-checkpoint-sqlite
itself; this module owns Atlas's own business-adjacent bookkeeping tables
(runs, tasks, companies, jobs, attempts, etc.) that the future Workspace
Agent business logic will read/write.

GitHub is NOT used as a high-frequency checkpoint database — see
atlas/persistence/github.py and docs/STATE_MODEL.md.

Phase 0.5 hardening (see docs/STATE_MODEL.md "Migration safety" and
"Concurrency"):
    - WAL journal mode (better for one-writer/occasional-reader local
      workloads than the default rollback-journal mode) with a busy
      timeout so concurrent local access waits briefly instead of
      immediately raising "database is locked".
    - Ordered, versioned migrations: each version's statements run in
      their own transaction; a failure rolls back that version's changes
      and raises MigrationError without recording it as applied, so a
      half-applied migration can never be silently treated as done.
    - No destructive migration exists yet. Before any future destructive
      migration is added, back up the file at `db_path` (a plain file
      copy is sufficient - SQLite files are self-contained) - see
      docs/STATE_MODEL.md.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import enum
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterator, Optional


class MigrationError(RuntimeError):
    """Raised when a schema migration fails; the failed migration's
    statements are rolled back and NOT recorded as applied."""


class LeaseMutationResult(str, enum.Enum):
    """Typed outcome of a fenced lease mutation (heartbeat/complete/release/
    reopen). ``APPLIED`` = the fenced conditional matched and changed the row;
    ``ALREADY_APPLIED_IDEMPOTENTLY`` = the SAME owner+token already effected an
    equivalent terminal state (a harmless duplicate delivery);
    ``STALE_TOKEN_REJECTED`` = a different owner/fencing token now holds the
    lease, so the caller is a stale worker and the mutation is refused (and
    audited); ``NOT_FOUND`` = no lease row exists for (run_id, coverage_id)."""

    APPLIED = "APPLIED"
    ALREADY_APPLIED_IDEMPOTENTLY = "ALREADY_APPLIED_IDEMPOTENTLY"
    STALE_TOKEN_REJECTED = "STALE_TOKEN_REJECTED"
    NOT_FOUND = "NOT_FOUND"


@dataclasses.dataclass(frozen=True)
class ChildCommitResult:
    """Outcome of an atomic fenced page commit (build spec 3 + 5): ``result``
    is the typed fence outcome and ``staged`` is the number of genuinely-new
    observations inserted (0 when the commit was refused as stale)."""

    result: LeaseMutationResult
    staged: int = 0

    @property
    def applied(self) -> bool:
        return self.result == LeaseMutationResult.APPLIED

    @property
    def rejected(self) -> bool:
        return self.result == LeaseMutationResult.STALE_TOKEN_REJECTED



# Ordered, versioned migrations. Each tuple is (version, description,
# statements-to-run-in-one-transaction). Versions MUST be applied in
# ascending order, each exactly once. Add new tuples with a new,
# incrementing version number for future schema changes - never edit a
# tuple that has already shipped (append-only).
_MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (
        1,
        "initial Atlas state schema (runs/tasks/companies/company_checks/"
        "sources/jobs/job_sources/attempts/failures/continuation/human_interventions)",
        [
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                controller TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                task_type TEXT NOT NULL,
                company TEXT,
                source TEXT,
                status TEXT NOT NULL,
                attempt_number INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS companies (
                company_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS company_checks (
                check_id TEXT PRIMARY KEY,
                company_id TEXT NOT NULL REFERENCES companies(company_id),
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                status TEXT NOT NULL,
                official_careers_url TEXT,
                final_url TEXT,
                page_title TEXT,
                career_platform TEXT,
                search_interface_accessible INTEGER,
                visible_job_count INTEGER,
                access_limitation TEXT,
                error TEXT,
                checked_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS sources (
                source_id TEXT PRIMARY KEY,
                source_type TEXT NOT NULL,
                name TEXT NOT NULL,
                base_url TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                company_id TEXT REFERENCES companies(company_id),
                title TEXT,
                url TEXT,
                dedupe_key TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS job_sources (
                job_id TEXT NOT NULL REFERENCES jobs(job_id),
                source_id TEXT NOT NULL REFERENCES sources(source_id),
                discovered_at TEXT NOT NULL,
                PRIMARY KEY (job_id, source_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS attempts (
                attempt_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(task_id),
                attempt_number INTEGER NOT NULL,
                result TEXT NOT NULL,
                error_category TEXT,
                error_detail TEXT,
                started_at TEXT NOT NULL,
                ended_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS failures (
                failure_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(task_id),
                error_category TEXT NOT NULL,
                error_detail TEXT,
                occurred_at TEXT NOT NULL,
                permanent INTEGER NOT NULL DEFAULT 0
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS continuation (
                run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
                thread_id TEXT NOT NULL,
                remaining_json TEXT NOT NULL DEFAULT '[]',
                completed_json TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS human_interventions (
                intervention_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(task_id),
                reason TEXT NOT NULL,
                status TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                resolved_at TEXT,
                notes TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_tasks_run_id ON tasks(run_id)",
            "CREATE INDEX IF NOT EXISTS idx_companies_run_id ON companies(run_id)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_run_id ON jobs(run_id)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_dedupe_key ON jobs(dedupe_key)",
            "CREATE INDEX IF NOT EXISTS idx_attempts_task_id ON attempts(task_id)",
        ],
    ),
    (
        2,
        "Phase 0.5: additional indexes for human_interventions/failures/job_sources "
        "lookups required by current generic state operations",
        [
            "CREATE INDEX IF NOT EXISTS idx_human_interventions_task_id ON human_interventions(task_id)",
            "CREATE INDEX IF NOT EXISTS idx_failures_task_id ON failures(task_id)",
            "CREATE INDEX IF NOT EXISTS idx_job_sources_source_id ON job_sources(source_id)",
        ],
    ),
    (
        3,
        "Phase 0.9: data-integrity canonical layer "
        "(canonical_jobs/job_observations/status_history/quarantine/"
        "data_integrity_runs/applied_operations) + idempotency support",
        [
            """
            CREATE TABLE IF NOT EXISTS canonical_jobs (
                canonical_id TEXT PRIMARY KEY,
                entity_type TEXT NOT NULL DEFAULT 'job',
                company TEXT,
                job_id TEXT,
                role TEXT,
                location TEXT,
                current_status TEXT NOT NULL DEFAULT 'UNKNOWN',
                first_seen TEXT,
                last_seen TEXT,
                match_score TEXT,
                source_url TEXT,
                official_apply_url TEXT,
                content_hash TEXT NOT NULL DEFAULT '',
                observation_count INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS job_observations (
                observation_id TEXT PRIMARY KEY,
                canonical_id TEXT NOT NULL REFERENCES canonical_jobs(canonical_id),
                record_id TEXT NOT NULL,
                source_file TEXT,
                sheet_name TEXT,
                discovery_source TEXT,
                observed_status TEXT,
                observed_at TEXT,
                content_hash TEXT NOT NULL DEFAULT '',
                provenance_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS status_history (
                history_id TEXT PRIMARY KEY,
                canonical_id TEXT NOT NULL REFERENCES canonical_jobs(canonical_id),
                axis TEXT NOT NULL DEFAULT 'canonical',
                from_status TEXT,
                to_status TEXT NOT NULL,
                reason TEXT,
                changed_at TEXT NOT NULL,
                context_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS quarantine (
                quarantine_id TEXT PRIMARY KEY,
                record_id TEXT NOT NULL,
                entity_type TEXT,
                identity_key TEXT,
                reason_code TEXT NOT NULL,
                severity TEXT,
                action TEXT,
                detail_json TEXT NOT NULL DEFAULT '{}',
                quarantined_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS data_integrity_runs (
                di_run_id TEXT PRIMARY KEY,
                source_file TEXT NOT NULL,
                source_hash TEXT,
                mapping_version TEXT,
                record_count INTEGER NOT NULL DEFAULT 0,
                quarantined_count INTEGER NOT NULL DEFAULT 0,
                created INTEGER NOT NULL DEFAULT 0,
                updated INTEGER NOT NULL DEFAULT 0,
                closed INTEGER NOT NULL DEFAULT 0,
                unchanged INTEGER NOT NULL DEFAULT 0,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                summary_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS applied_operations (
                op_key TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL,
                result_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_job_observations_canonical ON job_observations(canonical_id)",
            "CREATE INDEX IF NOT EXISTS idx_job_observations_record ON job_observations(record_id)",
            "CREATE INDEX IF NOT EXISTS idx_status_history_canonical ON status_history(canonical_id)",
            "CREATE INDEX IF NOT EXISTS idx_quarantine_record ON quarantine(record_id)",
            "CREATE INDEX IF NOT EXISTS idx_canonical_jobs_company ON canonical_jobs(company)",
        ],
    ),
    (
        4,
        "Phase 1A: source-engine coverage accounting (coverage_records), "
        "source health/yield history (source_health_history), and "
        "adapter_version/parser_version columns on job_observations",
        [
            """
            CREATE TABLE IF NOT EXISTS coverage_records (
                coverage_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                company TEXT,
                source_instance TEXT NOT NULL,
                source_type TEXT,
                lane TEXT,
                query_key TEXT,
                attempted INTEGER NOT NULL DEFAULT 0,
                completed INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'NOT_ATTEMPTED',
                jobs_found INTEGER NOT NULL DEFAULT 0,
                new_jobs INTEGER NOT NULL DEFAULT 0,
                changed_jobs INTEGER NOT NULL DEFAULT 0,
                closed_jobs INTEGER NOT NULL DEFAULT 0,
                started_at TEXT,
                completed_at TEXT,
                next_action TEXT NOT NULL DEFAULT 'NONE',
                detail_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS source_health_history (
                history_id TEXT PRIMARY KEY,
                source_instance TEXT NOT NULL,
                source_type TEXT,
                state TEXT NOT NULL,
                result_count INTEGER,
                expected_structure_present INTEGER,
                reason TEXT,
                observed_at TEXT NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
            "ALTER TABLE job_observations ADD COLUMN adapter_version TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE job_observations ADD COLUMN parser_version TEXT NOT NULL DEFAULT ''",
            "CREATE INDEX IF NOT EXISTS idx_coverage_run ON coverage_records(run_id)",
            "CREATE INDEX IF NOT EXISTS idx_coverage_instance ON coverage_records(source_instance)",
            "CREATE INDEX IF NOT EXISTS idx_health_hist_instance ON source_health_history(source_instance, observed_at)",
        ],
    ),
    (
        5,
        "Phase 1A.5: persistent company identity registry (company_registry/"
        "company_aliases), many-to-many company↔source relationships "
        "(company_source_relationships), and append-only source discovery "
        "provenance (source_discovery_observations)",
        [
            """
            CREATE TABLE IF NOT EXISTS company_registry (
                company_id TEXT PRIMARY KEY,
                canonical_name TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                identity_key TEXT NOT NULL,
                official_domain TEXT,
                careers_url TEXT,
                country TEXT,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                discovered_at TEXT,
                last_verified_at TEXT,
                provenance_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS company_aliases (
                alias_id TEXT PRIMARY KEY,
                company_id TEXT NOT NULL,
                alias TEXT NOT NULL,
                alias_key TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'explicit',
                created_at TEXT NOT NULL,
                UNIQUE(company_id, alias_key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS company_source_relationships (
                relationship_id TEXT PRIMARY KEY,
                company_id TEXT NOT NULL,
                instance_id TEXT NOT NULL,
                source_type TEXT NOT NULL,
                base_url TEXT,
                tenant TEXT,
                state TEXT NOT NULL DEFAULT 'DISCOVERED',
                confidence TEXT NOT NULL DEFAULT 'UNKNOWN',
                is_current INTEGER NOT NULL DEFAULT 1,
                discovered_at TEXT,
                updated_at TEXT NOT NULL,
                provenance_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(company_id, instance_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS source_discovery_observations (
                observation_id TEXT PRIMARY KEY,
                company_id TEXT NOT NULL,
                instance_id TEXT,
                method TEXT NOT NULL,
                input_url TEXT,
                resolved_url TEXT,
                detected_ats TEXT,
                tenant TEXT,
                confidence TEXT NOT NULL DEFAULT 'UNKNOWN',
                evidence_ref TEXT,
                verification_state TEXT NOT NULL DEFAULT 'UNVERIFIED',
                observed_at TEXT NOT NULL,
                detail_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_company_identity_key ON company_registry(identity_key)",
            "CREATE INDEX IF NOT EXISTS idx_company_domain ON company_registry(official_domain)",
            "CREATE INDEX IF NOT EXISTS idx_company_aliases_key ON company_aliases(alias_key)",
            "CREATE INDEX IF NOT EXISTS idx_company_aliases_company ON company_aliases(company_id)",
            "CREATE INDEX IF NOT EXISTS idx_csr_company ON company_source_relationships(company_id)",
            "CREATE INDEX IF NOT EXISTS idx_csr_instance ON company_source_relationships(instance_id)",
            "CREATE INDEX IF NOT EXISTS idx_sdo_company ON source_discovery_observations(company_id)",
        ],
    ),
    (
        6,
        "Phase 1B: first-class source_instances persistence (adapter_key/category/"
        "tenant+site/lifecycle/capabilities), query-signature-keyed source health "
        "history, append-only coverage_attempts (immutable diagnostics), and "
        "coverage_plans lifecycle (BUILDING/SEALED/FAILED + fingerprint).",
        [
            """
            CREATE TABLE IF NOT EXISTS source_instances (
                instance_id TEXT PRIMARY KEY,
                adapter_key TEXT NOT NULL,
                source_type TEXT NOT NULL,
                category TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                base_url TEXT,
                tenant TEXT,
                site TEXT,
                company_id TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                lifecycle_state TEXT NOT NULL DEFAULT 'ACTIVE',
                capability_additions_json TEXT NOT NULL DEFAULT '[]',
                capability_removals_json TEXT NOT NULL DEFAULT '[]',
                rate_policy_ref TEXT,
                auth_ref TEXT,
                provenance_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            # Query-signature granularity for health/yield history (P0-6/7.7).
            "ALTER TABLE source_health_history ADD COLUMN query_signature TEXT",
            "ALTER TABLE source_health_history ADD COLUMN lane TEXT",
            "ALTER TABLE source_health_history ADD COLUMN geography_group TEXT",
            "ALTER TABLE source_health_history ADD COLUMN search_mode TEXT",
            # Append-only immutable coverage attempt history (7.9) — never
            # upserted, so diagnostics are not lost to the resolved-state upsert.
            """
            CREATE TABLE IF NOT EXISTS coverage_attempts (
                attempt_id TEXT PRIMARY KEY,
                coverage_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                source_instance TEXT NOT NULL,
                query_signature TEXT,
                status TEXT NOT NULL,
                jobs_found INTEGER NOT NULL DEFAULT 0,
                new_jobs INTEGER NOT NULL DEFAULT 0,
                changed_jobs INTEGER NOT NULL DEFAULT 0,
                closed_jobs INTEGER NOT NULL DEFAULT 0,
                detail_json TEXT NOT NULL DEFAULT '{}',
                observed_at TEXT NOT NULL
            )
            """,
            # Coverage plan lifecycle so an unsealed/empty plan cannot
            # silently look complete and a failure differs from NO_WORK_DUE.
            """
            CREATE TABLE IF NOT EXISTS coverage_plans (
                run_id TEXT PRIMARY KEY,
                state TEXT NOT NULL DEFAULT 'BUILDING',
                no_work_due INTEGER NOT NULL DEFAULT 0,
                fingerprint TEXT,
                policy_fingerprint TEXT,
                failure_reason TEXT,
                sealed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_source_instances_family ON source_instances(adapter_key)",
            "CREATE INDEX IF NOT EXISTS idx_source_instances_company ON source_instances(company_id)",
            "CREATE INDEX IF NOT EXISTS idx_cov_attempts_coverage ON coverage_attempts(coverage_id, observed_at)",
            "CREATE INDEX IF NOT EXISTS idx_cov_attempts_run ON coverage_attempts(run_id)",
            "CREATE INDEX IF NOT EXISTS idx_health_hist_signature ON source_health_history(query_signature, observed_at)",
        ],
    ),
    (
        7,
        "Phase 1B.1: append-only raw discovery observation staging (build spec 12 / "
        "P0-15). Raw normalized source observations are staged here during DISCOVER "
        "and resolved into canonical_jobs only in DEDUPE/RECONCILE, preserving full "
        "provenance (run/coverage/attempt/query-signature) before canonicalization.",
        [
            """
            CREATE TABLE IF NOT EXISTS raw_discovery_observations (
                observation_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                coverage_id TEXT,
                attempt_id TEXT,
                query_signature TEXT,
                source_instance TEXT NOT NULL,
                source_family TEXT,
                source_job_id TEXT,
                source_url TEXT,
                canonical_url TEXT,
                company TEXT,
                title TEXT,
                location TEXT,
                lane TEXT,
                posted_at TEXT,
                is_active TEXT,
                content_hash TEXT NOT NULL,
                source_identity TEXT,
                raw_evidence_ref TEXT,
                adapter_version TEXT NOT NULL DEFAULT '',
                parser_version TEXT NOT NULL DEFAULT '',
                processing_status TEXT NOT NULL DEFAULT 'STAGED',
                canonical_id TEXT,
                observed_at TEXT NOT NULL,
                detail_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_raw_obs_run ON raw_discovery_observations(run_id)",
            "CREATE INDEX IF NOT EXISTS idx_raw_obs_status ON raw_discovery_observations(processing_status)",
            "CREATE INDEX IF NOT EXISTS idx_raw_obs_identity ON raw_discovery_observations(source_identity)",
            "CREATE INDEX IF NOT EXISTS idx_raw_obs_hash ON raw_discovery_observations(content_hash)",
            "CREATE INDEX IF NOT EXISTS idx_raw_obs_coverage ON raw_discovery_observations(coverage_id)",
        ],
    ),
    (
        8,
        "Phase 1C-A: atomic coverage task leases + append-only lease-event audit "
        "(build spec 7). Enables a bounded parallel worker pool to atomically claim "
        "the exact coverage child as the unique unit of work, prevents a second "
        "worker from claiming an unexpired lease, allows an expired lease to be "
        "reclaimed and a long task to heartbeat, and keeps a terminal task "
        "un-leasable — all in UTC, with every lease transition auditable.",
        [
            """
            CREATE TABLE IF NOT EXISTS coverage_leases (
                coverage_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                worker_id TEXT,
                status TEXT NOT NULL DEFAULT 'AVAILABLE',
                terminal INTEGER NOT NULL DEFAULT 0,
                attempt INTEGER NOT NULL DEFAULT 0,
                version INTEGER NOT NULL DEFAULT 0,
                lease_acquired_at TEXT,
                lease_expires_at TEXT,
                heartbeat_at TEXT,
                company TEXT,
                source_instance TEXT,
                tenant TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS coverage_lease_events (
                event_id TEXT PRIMARY KEY,
                coverage_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                worker_id TEXT,
                event TEXT NOT NULL,
                attempt INTEGER,
                lease_expires_at TEXT,
                at TEXT NOT NULL,
                detail_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_cov_leases_run ON coverage_leases(run_id, status, terminal)",
            "CREATE INDEX IF NOT EXISTS idx_cov_leases_expiry ON coverage_leases(run_id, lease_expires_at)",
            "CREATE INDEX IF NOT EXISTS idx_cov_lease_events_cov ON coverage_lease_events(coverage_id, at)",
            "CREATE INDEX IF NOT EXISTS idx_cov_lease_events_run ON coverage_lease_events(run_id, at)",
        ],
    ),
    (
        9,
        "Phase 1C-A corrective (build spec 6/7/9/10): run-SCOPED coverage/lease "
        "identity. Rebuild coverage_records and coverage_leases with a COMPOSITE "
        "primary key (run_id, coverage_id) so the SAME logical coverage_id is "
        "independently executable in two separate runs (a terminal lease in run-A "
        "cannot block run-B). Additive/data-preserving: existing v1-v8 rows are "
        "copied verbatim into the rebuilt tables. Adds coverage_page_state for "
        "durable run-scoped pagination cursors (a child cannot become terminal "
        "while a required next page/cursor remains).",
        [
            # --- coverage_records: rebuild with composite PK (run_id, coverage_id)
            """
            CREATE TABLE coverage_records_v9 (
                coverage_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                company TEXT,
                source_instance TEXT NOT NULL,
                source_type TEXT,
                lane TEXT,
                query_key TEXT,
                attempted INTEGER NOT NULL DEFAULT 0,
                completed INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'NOT_ATTEMPTED',
                jobs_found INTEGER NOT NULL DEFAULT 0,
                new_jobs INTEGER NOT NULL DEFAULT 0,
                changed_jobs INTEGER NOT NULL DEFAULT 0,
                closed_jobs INTEGER NOT NULL DEFAULT 0,
                started_at TEXT,
                completed_at TEXT,
                next_action TEXT NOT NULL DEFAULT 'NONE',
                detail_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (run_id, coverage_id)
            )
            """,
            "INSERT INTO coverage_records_v9 (coverage_id, run_id, company, source_instance, "
            "source_type, lane, query_key, attempted, completed, status, jobs_found, new_jobs, "
            "changed_jobs, closed_jobs, started_at, completed_at, next_action, detail_json, "
            "created_at, updated_at) SELECT coverage_id, run_id, company, source_instance, "
            "source_type, lane, query_key, attempted, completed, status, jobs_found, new_jobs, "
            "changed_jobs, closed_jobs, started_at, completed_at, next_action, detail_json, "
            "created_at, updated_at FROM coverage_records",
            "DROP TABLE coverage_records",
            "ALTER TABLE coverage_records_v9 RENAME TO coverage_records",
            "CREATE INDEX IF NOT EXISTS idx_coverage_run ON coverage_records(run_id)",
            "CREATE INDEX IF NOT EXISTS idx_coverage_instance ON coverage_records(source_instance)",
            # --- coverage_leases: rebuild with composite PK (run_id, coverage_id)
            """
            CREATE TABLE coverage_leases_v9 (
                coverage_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                worker_id TEXT,
                status TEXT NOT NULL DEFAULT 'AVAILABLE',
                terminal INTEGER NOT NULL DEFAULT 0,
                attempt INTEGER NOT NULL DEFAULT 0,
                version INTEGER NOT NULL DEFAULT 0,
                lease_acquired_at TEXT,
                lease_expires_at TEXT,
                heartbeat_at TEXT,
                company TEXT,
                source_instance TEXT,
                tenant TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (run_id, coverage_id)
            )
            """,
            "INSERT INTO coverage_leases_v9 (coverage_id, run_id, worker_id, status, terminal, "
            "attempt, version, lease_acquired_at, lease_expires_at, heartbeat_at, company, "
            "source_instance, tenant, created_at, updated_at) SELECT coverage_id, run_id, worker_id, "
            "status, terminal, attempt, version, lease_acquired_at, lease_expires_at, heartbeat_at, "
            "company, source_instance, tenant, created_at, updated_at FROM coverage_leases",
            "DROP TABLE coverage_leases",
            "ALTER TABLE coverage_leases_v9 RENAME TO coverage_leases",
            "CREATE INDEX IF NOT EXISTS idx_cov_leases_run ON coverage_leases(run_id, status, terminal)",
            "CREATE INDEX IF NOT EXISTS idx_cov_leases_expiry ON coverage_leases(run_id, lease_expires_at)",
            # --- durable run-scoped pagination cursor state (build spec 10) ----
            """
            CREATE TABLE IF NOT EXISTS coverage_page_state (
                run_id TEXT NOT NULL,
                coverage_id TEXT NOT NULL,
                page_index INTEGER NOT NULL,
                cursor TEXT,
                status TEXT NOT NULL DEFAULT 'PENDING',
                has_more INTEGER NOT NULL DEFAULT 0,
                results_count INTEGER NOT NULL DEFAULT 0,
                total_reported INTEGER,
                request_fingerprint TEXT,
                attempt_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (run_id, coverage_id, page_index)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_cov_page_run ON coverage_page_state(run_id, coverage_id)",
            "CREATE INDEX IF NOT EXISTS idx_cov_page_status ON coverage_page_state(run_id, status)",
        ],
    ),
    (
        10,
        "Phase 1C-A FINAL stabilization (build spec 6/10): (a) an explicit "
        "observation-revision link on raw_discovery_observations "
        "(parent_observation_id + revision_kind) so a hydrated DETAIL revision "
        "references its source SEARCH observation WITHOUT misusing canonical_id "
        "to store an observation id; and (b) an append-only verification_decisions "
        "table so every per-job verification decision (selected evidence "
        "revisions, level, lifecycle, freshness, closure, classifier + policy "
        "version) is DURABLE and the report is derived from the persisted "
        "decision, never re-derived from an adapter label. Additive/data-"
        "preserving: existing rows default parent_observation_id NULL / "
        "revision_kind 'SEARCH'.",
        [
            "ALTER TABLE raw_discovery_observations ADD COLUMN parent_observation_id TEXT",
            "ALTER TABLE raw_discovery_observations ADD COLUMN revision_kind TEXT NOT NULL DEFAULT 'SEARCH'",
            "CREATE INDEX IF NOT EXISTS idx_raw_obs_parent ON raw_discovery_observations(parent_observation_id)",
            """
            CREATE TABLE IF NOT EXISTS verification_decisions (
                decision_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                canonical_id TEXT,
                evidence_revision_ids TEXT NOT NULL DEFAULT '[]',
                verification_level TEXT NOT NULL,
                lifecycle_result TEXT,
                freshness_result TEXT,
                closure_evidence TEXT,
                classifier_version TEXT NOT NULL DEFAULT '',
                policy_fingerprint TEXT NOT NULL DEFAULT '',
                reasoning_ref TEXT,
                decided_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_verif_dec_run ON verification_decisions(run_id)",
            "CREATE INDEX IF NOT EXISTS idx_verif_dec_canonical ON verification_decisions(run_id, canonical_id)",
        ],
    ),
    (
        11,
        "Phase 1C-B official career-site coverage (build spec 12): additive, "
        "data-preserving tables for (a) discovered career ENTRY POINTS "
        "(append-only, provenance + trust); (b) a reusable, revalidate-before-"
        "trust generic career-site PROFILE/recipe per source instance (route "
        "kind, selectors, link patterns, evidence, confidence, recipe/parser "
        "version, health); (c) append-only ROUTE CLASSIFICATIONS; and (d) sealed "
        "career PILOT runs (config hash + results). No existing table/column is "
        "modified; the four ATS routes and every prior run are untouched.",
        [
            """
            CREATE TABLE IF NOT EXISTS career_entry_points (
                entry_id TEXT PRIMARY KEY,
                company_id TEXT,
                entry_url TEXT NOT NULL,
                label TEXT,
                discovery_method TEXT NOT NULL DEFAULT 'UNKNOWN',
                trusted INTEGER NOT NULL DEFAULT 0,
                trust_kind TEXT,
                confidence REAL NOT NULL DEFAULT 0.0,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                observed_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_career_entry_company ON career_entry_points(company_id)",
            "CREATE INDEX IF NOT EXISTS idx_career_entry_url ON career_entry_points(entry_url)",
            """
            CREATE TABLE IF NOT EXISTS career_site_profiles (
                source_instance_id TEXT PRIMARY KEY,
                profile_id TEXT NOT NULL,
                company_id TEXT,
                entry_url TEXT NOT NULL,
                route_kind TEXT NOT NULL,
                recipe_json TEXT NOT NULL DEFAULT '{}',
                confidence REAL NOT NULL DEFAULT 0.0,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                recipe_version TEXT NOT NULL DEFAULT '1.0.0',
                parser_version TEXT NOT NULL DEFAULT '',
                health TEXT NOT NULL DEFAULT 'UNVALIDATED',
                last_success_at TEXT,
                last_validated_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_career_profile_company ON career_site_profiles(company_id)",
            "CREATE INDEX IF NOT EXISTS idx_career_profile_health ON career_site_profiles(health)",
            """
            CREATE TABLE IF NOT EXISTS career_route_classifications (
                classification_id TEXT PRIMARY KEY,
                company_id TEXT,
                source_instance_id TEXT,
                entry_url TEXT NOT NULL,
                route_kind TEXT NOT NULL,
                fingerprint_family TEXT,
                confidence REAL NOT NULL DEFAULT 0.0,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                classified_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_career_route_instance ON career_route_classifications(source_instance_id)",
            """
            CREATE TABLE IF NOT EXISTS career_pilot_runs (
                run_id TEXT PRIMARY KEY,
                config_hash TEXT NOT NULL,
                config_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'SEALED',
                results_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
        ],
    ),
    (
        12,
        "Phase 1D market discovery + adaptive search (build spec 6/10/12/13/17): "
        "additive, data-preserving tables for (a) append-only PORTAL job LEADS "
        "(LinkedIn/Naukri, run-scoped, never auto-official); (b) MARKET CAMPAIGN "
        "and append-only sealed WAVE definitions; (c) wave DEFICITS and expansion "
        "reasons; (d) bounded QUERY VARIANTS + policy versions; (e) DYNAMIC-COMPANY "
        "provenance; (f) PORTAL->OFFICIAL relationships; and (g) a run-scoped global "
        "CAMPAIGN BUDGET ledger. Also adds two additive columns to career_entry_points "
        "(validated / reachability) so a common-path LEAD is distinguished from a "
        "reachability-validated entry. No existing table/column is modified.",
        [
            # -- (a) career entry-point reachability (LEAD vs validated) --------
            "ALTER TABLE career_entry_points ADD COLUMN validated INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE career_entry_points ADD COLUMN reachability TEXT",
            # -- (b) append-only portal job leads -------------------------------
            """
            CREATE TABLE IF NOT EXISTS portal_leads (
                lead_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                campaign_id TEXT,
                wave_id TEXT,
                source_family TEXT NOT NULL,
                source_instance TEXT,
                portal_job_id TEXT,
                canonical_url TEXT,
                title TEXT,
                company_name TEXT,
                company_id TEXT,
                location TEXT,
                posted_text TEXT,
                posted_provenance TEXT,
                work_mode TEXT,
                lane TEXT,
                result_query TEXT,
                result_page INTEGER,
                result_cursor TEXT,
                salary_text TEXT,
                experience_text TEXT,
                official_apply_url TEXT,
                company_url TEXT,
                verification_state TEXT NOT NULL DEFAULT 'PORTAL_CURRENT_LEAD',
                raw_evidence_ref TEXT,
                health_findings_json TEXT NOT NULL DEFAULT '[]',
                detail_json TEXT NOT NULL DEFAULT '{}',
                content_hash TEXT,
                observed_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_portal_leads_run ON portal_leads(run_id)",
            "CREATE INDEX IF NOT EXISTS idx_portal_leads_family ON portal_leads(source_family)",
            "CREATE INDEX IF NOT EXISTS idx_portal_leads_company ON portal_leads(company_id)",
            "CREATE INDEX IF NOT EXISTS idx_portal_leads_state ON portal_leads(verification_state)",
            # -- (c) market campaign --------------------------------------------
            """
            CREATE TABLE IF NOT EXISTS market_campaigns (
                campaign_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                policy_fingerprint TEXT,
                candidate_fingerprint TEXT,
                mode TEXT NOT NULL DEFAULT 'DELTA',
                budgets_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'SEALED',
                current_wave INTEGER NOT NULL DEFAULT 0,
                terminal_reason TEXT,
                config_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_campaign_run ON market_campaigns(run_id)",
            # -- (c) append-only sealed waves -----------------------------------
            """
            CREATE TABLE IF NOT EXISTS market_waves (
                wave_id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                wave_index INTEGER NOT NULL,
                parent_wave_id TEXT,
                seal_hash TEXT,
                status TEXT NOT NULL DEFAULT 'PLANNED',
                deficit_reason TEXT,
                tasks_json TEXT NOT NULL DEFAULT '[]',
                terminal_reason TEXT,
                created_at TEXT NOT NULL,
                sealed_at TEXT,
                updated_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_waves_campaign ON market_waves(campaign_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_waves_campaign_index ON market_waves(campaign_id, wave_index)",
            # -- (d) wave deficits ----------------------------------------------
            """
            CREATE TABLE IF NOT EXISTS wave_deficits (
                deficit_id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                wave_id TEXT NOT NULL,
                deficit_kind TEXT NOT NULL,
                lane TEXT,
                geography TEXT,
                source_family TEXT,
                detail_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_deficits_campaign ON wave_deficits(campaign_id)",
            # -- (d) bounded query variants -------------------------------------
            """
            CREATE TABLE IF NOT EXISTS query_variants (
                variant_id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                wave_id TEXT,
                lane TEXT,
                geography TEXT,
                source_family TEXT,
                query TEXT NOT NULL,
                origin TEXT NOT NULL DEFAULT 'SYNONYM',
                policy_version TEXT,
                reason TEXT,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_variants_campaign ON query_variants(campaign_id)",
            # -- (e) dynamic-company provenance ---------------------------------
            """
            CREATE TABLE IF NOT EXISTS dynamic_company_provenance (
                provenance_id TEXT PRIMARY KEY,
                company_id TEXT,
                run_id TEXT,
                discovery_source TEXT NOT NULL,
                portal_lead_id TEXT,
                normalized_name TEXT,
                resolved_domain TEXT,
                resolution_status TEXT NOT NULL DEFAULT 'DYNAMICALLY_DISCOVERED',
                evidence_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_dynco_company ON dynamic_company_provenance(company_id)",
            "CREATE INDEX IF NOT EXISTS idx_dynco_status ON dynamic_company_provenance(resolution_status)",
            # -- (f) portal -> official relationships ---------------------------
            """
            CREATE TABLE IF NOT EXISTS portal_official_links (
                link_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                portal_lead_id TEXT NOT NULL,
                canonical_id TEXT,
                official_observation_id TEXT,
                match_kind TEXT NOT NULL DEFAULT 'NONE',
                confidence REAL NOT NULL DEFAULT 0.0,
                verification_state TEXT NOT NULL DEFAULT 'PORTAL_CURRENT_LEAD',
                ambiguous INTEGER NOT NULL DEFAULT 0,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_polink_run ON portal_official_links(run_id)",
            "CREATE INDEX IF NOT EXISTS idx_polink_lead ON portal_official_links(portal_lead_id)",
            # -- (g) run-scoped global campaign budget ledger -------------------
            """
            CREATE TABLE IF NOT EXISTS campaign_budget (
                campaign_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                http_calls INTEGER NOT NULL DEFAULT 0,
                browser_calls INTEGER NOT NULL DEFAULT 0,
                llm_calls INTEGER NOT NULL DEFAULT 0,
                results INTEGER NOT NULL DEFAULT 0,
                max_http INTEGER,
                max_browser INTEGER,
                max_llm INTEGER,
                max_results INTEGER,
                max_wall_clock_s REAL,
                started_at TEXT,
                updated_at TEXT NOT NULL
            )
            """,
        ],
    ),
    (
        13,
        "stateless VS Code hunt control plane",
        [
            """
            CREATE TABLE IF NOT EXISTS vscode_hunt_runs (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                company_count INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS vscode_hunt_tasks (
                task_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES vscode_hunt_runs(run_id),
                company_id TEXT NOT NULL,
                company_name TEXT NOT NULL,
                official_domain TEXT NOT NULL,
                careers_url TEXT,
                lanes_json TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt_number INTEGER NOT NULL DEFAULT 0,
                UNIQUE(run_id, company_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS vscode_hunt_attempts (
                attempt_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES vscode_hunt_tasks(task_id),
                attempt_number INTEGER NOT NULL,
                backend TEXT NOT NULL,
                parent_attempt_id TEXT,
                status TEXT NOT NULL,
                result_hash TEXT,
                result_path TEXT,
                missing_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_vscode_hunt_tasks_run ON vscode_hunt_tasks(run_id)",
        ],
    ),
]

SCHEMA_VERSION = max(version for version, _, _ in _MIGRATIONS)

_BOOTSTRAP_STATEMENT = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    applied_at TEXT NOT NULL
)
"""


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class StateStore:
    """Thin, dependency-free wrapper around the Atlas SQLite state DB.

    Not thread-safe by design (matches the existing proven LangGraph
    SqliteSaver usage pattern in this codebase) - callers should use one
    StateStore per process/thread, matching how BrowserManager and the
    orchestration graph are used elsewhere in Atlas.

    Concurrency settings (Phase 0.5): WAL journal mode + a busy timeout so
    a second short-lived reader (e.g. `atlas status`/`atlas doctor`
    inspecting the DB while a run is active) waits briefly instead of
    immediately failing with "database is locked", matching Atlas's
    actual expected local workload (one writer process, occasional
    read-only CLI inspection) without prematurely over-engineering for a
    high-concurrency workload Atlas does not have.
    """

    def __init__(self, db_path: Path, busy_timeout_ms: int = 5000):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        self._migrate()

    def _migrate(self) -> None:
        # Bootstrap: the migration-tracking table itself must exist
        # before we can query it, and its own creation is idempotent and
        # side-effect-free, so it is not tracked as a numbered migration.
        with self._conn:
            self._conn.execute(_BOOTSTRAP_STATEMENT)

        applied = self._conn.execute(
            "SELECT MAX(version) AS v FROM schema_migrations"
        ).fetchone()
        current_version = applied["v"] if applied and applied["v"] is not None else 0

        for version, description, statements in _MIGRATIONS:
            if version <= current_version:
                continue  # already applied - migrations run exactly once
            try:
                with self._conn:  # single transaction per migration version
                    for statement in statements:
                        self._conn.execute(statement)
                    self._conn.execute(
                        "INSERT INTO schema_migrations (version, description, applied_at) VALUES (?, ?, ?)",
                        (version, description, _utcnow()),
                    )
            except sqlite3.Error as exc:
                # `with self._conn:` already rolled back this version's
                # partial changes; do NOT record it as applied.
                raise MigrationError(
                    f"Migration {version} ({description}) failed and was rolled back: {exc}"
                ) from exc

    def close(self) -> None:
        self._conn.close()

    def schema_version(self) -> int:
        row = self._conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
        return int(row["v"]) if row and row["v"] is not None else 0

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------
    def create_run(self, run_id: str, controller: str, metadata: Optional[dict] = None) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO runs (run_id, controller, status, started_at, metadata_json) "
                "VALUES (?, ?, 'RUNNING', ?, ?)",
                (run_id, controller, _utcnow(), json.dumps(metadata or {})),
            )

    def complete_run(self, run_id: str, status: str = "COMPLETE") -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE runs SET status = ?, completed_at = ? WHERE run_id = ?",
                (status, _utcnow(), run_id),
            )

    def get_run(self, run_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------
    def upsert_task(
        self,
        task_id: str,
        run_id: str,
        task_type: str,
        status: str,
        company: Optional[str] = None,
        source: Optional[str] = None,
        attempt_number: int = 0,
        payload: Optional[dict] = None,
    ) -> None:
        now = _utcnow()
        with self._conn:
            existing = self._conn.execute(
                "SELECT task_id FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if existing:
                self._conn.execute(
                    "UPDATE tasks SET status=?, attempt_number=?, updated_at=?, payload_json=? "
                    "WHERE task_id=?",
                    (status, attempt_number, now, json.dumps(payload or {}), task_id),
                )
            else:
                self._conn.execute(
                    "INSERT INTO tasks (task_id, run_id, task_type, company, source, status, "
                    "attempt_number, created_at, updated_at, payload_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        task_id,
                        run_id,
                        task_type,
                        company,
                        source,
                        status,
                        attempt_number,
                        now,
                        now,
                        json.dumps(payload or {}),
                    ),
                )

    def get_task(self, task_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()

    def list_tasks(self, run_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM tasks WHERE run_id = ?", (run_id,)
        ).fetchall()

    # ------------------------------------------------------------------
    # Attempts
    # ------------------------------------------------------------------
    def record_attempt(
        self,
        attempt_id: str,
        task_id: str,
        attempt_number: int,
        result: str,
        error_category: Optional[str] = None,
        error_detail: Optional[str] = None,
    ) -> None:
        now = _utcnow()
        with self._conn:
            self._conn.execute(
                "INSERT INTO attempts (attempt_id, task_id, attempt_number, result, "
                "error_category, error_detail, started_at, ended_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (attempt_id, task_id, attempt_number, result, error_category, error_detail, now, now),
            )

    def list_attempts(self, task_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM attempts WHERE task_id = ? ORDER BY attempt_number",
            (task_id,),
        ).fetchall()

    # ------------------------------------------------------------------
    # Human interventions
    # ------------------------------------------------------------------
    def request_human_intervention(
        self, intervention_id: str, task_id: str, reason: str
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO human_interventions (intervention_id, task_id, reason, status, requested_at) "
                "VALUES (?, ?, ?, 'WAITING_FOR_HUMAN', ?)",
                (intervention_id, task_id, reason, _utcnow()),
            )

    def resolve_human_intervention(self, intervention_id: str, notes: str = "") -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE human_interventions SET status='RESOLVED', resolved_at=?, notes=? "
                "WHERE intervention_id=?",
                (_utcnow(), notes, intervention_id),
            )

    def intervention_status(self, intervention_id: str) -> Optional[str]:
        """Return the durable status ('WAITING_FOR_HUMAN' or 'RESOLVED')
        of a previously-requested intervention, or None if it was never
        requested. Used to make finalization idempotent across separate
        AtlasRuntime process instances (e.g. run -> crash -> resume)."""
        row = self._conn.execute(
            "SELECT status FROM human_interventions WHERE intervention_id = ? "
            "ORDER BY requested_at DESC LIMIT 1",
            (intervention_id,),
        ).fetchone()
        return row["status"] if row is not None else None

    # ------------------------------------------------------------------
    # Continuation (durable "what remains" bookkeeping alongside LangGraph)
    # ------------------------------------------------------------------
    def save_continuation(
        self, run_id: str, thread_id: str, remaining: list[str], completed: list[str]
    ) -> None:
        now = _utcnow()
        with self._conn:
            self._conn.execute(
                "INSERT INTO continuation (run_id, thread_id, remaining_json, completed_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET thread_id=excluded.thread_id, "
                "remaining_json=excluded.remaining_json, completed_json=excluded.completed_json, "
                "updated_at=excluded.updated_at",
                (run_id, thread_id, json.dumps(remaining), json.dumps(completed), now),
            )

    def get_continuation(self, run_id: str) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM continuation WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "thread_id": row["thread_id"],
            "remaining": json.loads(row["remaining_json"]),
            "completed": json.loads(row["completed_json"]),
            "updated_at": row["updated_at"],
        }

    # ------------------------------------------------------------------
    # Phase 0.9 — idempotent transaction support
    # ------------------------------------------------------------------
    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Explicit multi-statement transaction with guaranteed rollback.

        Everything executed inside the ``with`` block commits atomically on
        clean exit, and is fully rolled back if any exception propagates -
        so a partially-applied reconciliation can never be persisted.
        """
        conn = self._conn
        # Finish any implicit transaction first so BEGIN is well-defined.
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()

    @contextlib.contextmanager
    def _auto(self) -> Iterator[sqlite3.Connection]:
        """Commit standalone writes, but defer to an enclosing
        :meth:`transaction` when one is active.

        A single v3 write called on its own persists immediately; the same
        write executed inside ``with store.transaction():`` participates in
        that outer transaction and is committed/rolled back with it.
        """
        conn = self._conn
        autocommit = not conn.in_transaction
        try:
            yield conn
        except BaseException:
            if autocommit:
                conn.rollback()
            raise
        else:
            if autocommit:
                conn.commit()

    def operation_applied(self, op_key: str) -> Optional[dict[str, Any]]:
        """Return the recorded result for an idempotency key, or None.

        Lets a caller make a whole operation idempotent: check this first
        and skip re-doing work that already completed for ``op_key``."""
        row = self._conn.execute(
            "SELECT result_json FROM applied_operations WHERE op_key = ?", (op_key,)
        ).fetchone()
        return json.loads(row["result_json"]) if row is not None else None

    def mark_operation(self, op_key: str, result: Optional[dict] = None) -> None:
        with self._auto() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO applied_operations (op_key, applied_at, result_json) "
                "VALUES (?, ?, ?)",
                (op_key, _utcnow(), json.dumps(result or {}, sort_keys=True)),
            )

    # ------------------------------------------------------------------
    # Phase 0.9 — canonical jobs
    # ------------------------------------------------------------------
    def upsert_canonical_job(
        self,
        canonical_id: str,
        *,
        entity_type: str = "job",
        company: Optional[str] = None,
        job_id: Optional[str] = None,
        role: Optional[str] = None,
        location: Optional[str] = None,
        status: str = "UNKNOWN",
        first_seen: Optional[str] = None,
        last_seen: Optional[str] = None,
        match_score: Optional[Any] = None,
        source_url: Optional[str] = None,
        official_apply_url: Optional[str] = None,
        content_hash: str = "",
        payload: Optional[dict] = None,
    ) -> str:
        """Insert or update a canonical job. Returns 'created', 'updated'
        or 'unchanged'. Idempotent on ``content_hash``: re-applying an
        identical record touches nothing."""
        now = _utcnow()
        existing = self._conn.execute(
            "SELECT content_hash, observation_count FROM canonical_jobs WHERE canonical_id = ?",
            (canonical_id,),
        ).fetchone()
        if existing is None:
            with self._auto() as conn:
                conn.execute(
                    "INSERT INTO canonical_jobs (canonical_id, entity_type, company, job_id, role, "
                    "location, current_status, first_seen, last_seen, match_score, source_url, "
                    "official_apply_url, content_hash, observation_count, payload_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
                    (
                        canonical_id, entity_type, company, job_id, role, location, status,
                        first_seen, last_seen, None if match_score is None else str(match_score),
                        source_url, official_apply_url, content_hash,
                        json.dumps(payload or {}, sort_keys=True), now, now,
                    ),
                )
            return "created"
        if existing["content_hash"] == content_hash:
            return "unchanged"
        with self._auto() as conn:
            conn.execute(
                "UPDATE canonical_jobs SET company=?, job_id=?, role=?, location=?, current_status=?, "
                "first_seen=?, last_seen=?, match_score=?, source_url=?, official_apply_url=?, "
                "content_hash=?, payload_json=?, updated_at=? WHERE canonical_id=?",
                (
                    company, job_id, role, location, status, first_seen, last_seen,
                    None if match_score is None else str(match_score), source_url,
                    official_apply_url, content_hash, json.dumps(payload or {}, sort_keys=True),
                    now, canonical_id,
                ),
            )
        return "updated"

    def set_canonical_status(self, canonical_id: str, status: str) -> None:
        with self._auto() as conn:
            conn.execute(
                "UPDATE canonical_jobs SET current_status=?, updated_at=? WHERE canonical_id=?",
                (status, _utcnow(), canonical_id),
            )

    def get_canonical_job(self, canonical_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM canonical_jobs WHERE canonical_id = ?", (canonical_id,)
        ).fetchone()

    def list_canonical_jobs(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM canonical_jobs ORDER BY canonical_id"
        ).fetchall()

    def count_canonical_jobs(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) AS n FROM canonical_jobs").fetchone()["n"])

    # ------------------------------------------------------------------
    # Phase 0.9 — observations
    # ------------------------------------------------------------------
    def add_observation(
        self,
        observation_id: str,
        canonical_id: str,
        record_id: str,
        *,
        source_file: Optional[str] = None,
        sheet_name: Optional[str] = None,
        discovery_source: Optional[str] = None,
        observed_status: Optional[str] = None,
        observed_at: Optional[str] = None,
        content_hash: str = "",
        provenance: Optional[dict] = None,
        adapter_version: str = "",
        parser_version: str = "",
    ) -> bool:
        """Append an observation. Idempotent: a duplicate ``observation_id``
        is ignored (returns False). Increments the canonical observation
        count only for genuinely new observations.

        ``adapter_version``/``parser_version`` are persisted alongside the
        observation so a later parser change never has to mutate historical
        rows to know which parser produced them (Phase 1A)."""
        with self._auto() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO job_observations (observation_id, canonical_id, record_id, "
                "source_file, sheet_name, discovery_source, observed_status, observed_at, "
                "content_hash, provenance_json, adapter_version, parser_version, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    observation_id, canonical_id, record_id, source_file, sheet_name,
                    discovery_source, observed_status, observed_at, content_hash,
                    json.dumps(provenance or {}, sort_keys=True), adapter_version,
                    parser_version, _utcnow(),
                ),
            )
            if cur.rowcount:
                conn.execute(
                    "UPDATE canonical_jobs SET observation_count = observation_count + 1 "
                    "WHERE canonical_id = ?",
                    (canonical_id,),
                )
                return True
        return False

    def list_observations(self, canonical_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM job_observations WHERE canonical_id = ? ORDER BY observation_id",
            (canonical_id,),
        ).fetchall()

    def count_observations(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) AS n FROM job_observations").fetchone()["n"])

    # ------------------------------------------------------------------
    # Phase 0.9 — status history
    # ------------------------------------------------------------------
    def record_status_change(
        self,
        history_id: str,
        canonical_id: str,
        to_status: str,
        *,
        from_status: Optional[str] = None,
        reason: str = "",
        axis: str = "canonical",
        context: Optional[dict] = None,
    ) -> bool:
        """Record a status transition (idempotent on ``history_id``)."""
        with self._auto() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO status_history (history_id, canonical_id, axis, from_status, "
                "to_status, reason, changed_at, context_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    history_id, canonical_id, axis, from_status, to_status, reason,
                    _utcnow(), json.dumps(context or {}, sort_keys=True),
                ),
            )
        return bool(cur.rowcount)

    def list_status_history(self, canonical_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM status_history WHERE canonical_id = ? ORDER BY changed_at, history_id",
            (canonical_id,),
        ).fetchall()

    # ------------------------------------------------------------------
    # Phase 0.9 — quarantine
    # ------------------------------------------------------------------
    def add_quarantine(
        self,
        quarantine_id: str,
        record_id: str,
        reason_code: str,
        *,
        entity_type: Optional[str] = None,
        identity_key: Optional[str] = None,
        severity: Optional[str] = None,
        action: Optional[str] = None,
        detail: Optional[dict] = None,
    ) -> bool:
        """Quarantine a record (idempotent on ``quarantine_id``)."""
        with self._auto() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO quarantine (quarantine_id, record_id, entity_type, identity_key, "
                "reason_code, severity, action, detail_json, quarantined_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    quarantine_id, record_id, entity_type, identity_key, reason_code,
                    severity, action, json.dumps(detail or {}, sort_keys=True), _utcnow(),
                ),
            )
        return bool(cur.rowcount)

    def list_quarantine(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM quarantine ORDER BY quarantine_id"
        ).fetchall()

    def count_quarantine(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) AS n FROM quarantine").fetchone()["n"])

    # ------------------------------------------------------------------
    # Phase 0.9 — data-integrity run bookkeeping
    # ------------------------------------------------------------------
    def start_data_integrity_run(
        self,
        di_run_id: str,
        source_file: str,
        *,
        source_hash: Optional[str] = None,
        mapping_version: Optional[str] = None,
    ) -> None:
        with self._auto() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO data_integrity_runs (di_run_id, source_file, source_hash, "
                "mapping_version, started_at) VALUES (?, ?, ?, ?, ?)",
                (di_run_id, source_file, source_hash, mapping_version, _utcnow()),
            )

    def complete_data_integrity_run(self, di_run_id: str, summary: dict[str, Any]) -> None:
        with self._auto() as conn:
            conn.execute(
                "UPDATE data_integrity_runs SET record_count=?, quarantined_count=?, created=?, "
                "updated=?, closed=?, unchanged=?, completed_at=?, summary_json=? WHERE di_run_id=?",
                (
                    int(summary.get("record_count", 0)),
                    int(summary.get("quarantined", 0)),
                    int(summary.get("created", 0)),
                    int(summary.get("updated", 0)),
                    int(summary.get("closed", 0)),
                    int(summary.get("unchanged", 0)),
                    _utcnow(),
                    json.dumps(summary, sort_keys=True),
                    di_run_id,
                ),
            )

    def get_data_integrity_run(self, di_run_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM data_integrity_runs WHERE di_run_id = ?", (di_run_id,)
        ).fetchone()

    # ------------------------------------------------------------------
    # Phase 1A — coverage accounting
    # ------------------------------------------------------------------
    def upsert_coverage(
        self,
        coverage_id: str,
        run_id: str,
        source_instance: str,
        *,
        company: Optional[str] = None,
        source_type: Optional[str] = None,
        lane: Optional[str] = None,
        query_key: Optional[str] = None,
        attempted: bool = False,
        completed: bool = False,
        status: str = "NOT_ATTEMPTED",
        jobs_found: int = 0,
        new_jobs: int = 0,
        changed_jobs: int = 0,
        closed_jobs: int = 0,
        started_at: Optional[str] = None,
        completed_at: Optional[str] = None,
        next_action: str = "NONE",
        detail: Optional[dict] = None,
    ) -> None:
        """Insert or update one coverage record (idempotent on
        ``coverage_id``). This is how Atlas proves it attempted every
        planned source/company/lane rather than stopping after N jobs."""
        now = _utcnow()
        with self._auto() as conn:
            self._upsert_coverage(
                conn, coverage_id, run_id, source_instance, company=company, source_type=source_type,
                lane=lane, query_key=query_key, attempted=attempted, completed=completed, status=status,
                jobs_found=jobs_found, new_jobs=new_jobs, changed_jobs=changed_jobs, closed_jobs=closed_jobs,
                started_at=started_at, completed_at=completed_at, next_action=next_action, detail=detail, now=now,
            )

    def _upsert_coverage(
        self, conn, coverage_id, run_id, source_instance, *, company=None, source_type=None,
        lane=None, query_key=None, attempted=False, completed=False, status="NOT_ATTEMPTED",
        jobs_found=0, new_jobs=0, changed_jobs=0, closed_jobs=0, started_at=None, completed_at=None,
        next_action="NONE", detail=None, now=None,
    ):
        """Conn-scoped coverage upsert shared by the standalone call and the
        atomic fenced child-completion commit."""
        now = now or _utcnow()
        conn.execute(
            "INSERT INTO coverage_records (coverage_id, run_id, company, source_instance, "
            "source_type, lane, query_key, attempted, completed, status, jobs_found, new_jobs, "
            "changed_jobs, closed_jobs, started_at, completed_at, next_action, detail_json, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id, coverage_id) DO UPDATE SET company=excluded.company, "
            "source_type=excluded.source_type, lane=excluded.lane, query_key=excluded.query_key, "
            "attempted=excluded.attempted, completed=excluded.completed, status=excluded.status, "
            "jobs_found=excluded.jobs_found, new_jobs=excluded.new_jobs, changed_jobs=excluded.changed_jobs, "
            "closed_jobs=excluded.closed_jobs, started_at=excluded.started_at, "
            "completed_at=excluded.completed_at, next_action=excluded.next_action, "
            "detail_json=excluded.detail_json, updated_at=excluded.updated_at",
            (
                coverage_id, run_id, company, source_instance, source_type, lane, query_key,
                1 if attempted else 0, 1 if completed else 0, status, int(jobs_found),
                int(new_jobs), int(changed_jobs), int(closed_jobs), started_at, completed_at,
                next_action, json.dumps(detail or {}, sort_keys=True), now, now,
            ),
        )

    def get_coverage(self, coverage_id: str, run_id: Optional[str] = None) -> Optional[sqlite3.Row]:
        """Fetch one coverage record. ``run_id`` disambiguates the same logical
        ``coverage_id`` across runs (composite PK is ``(run_id, coverage_id)``);
        it is optional only for legacy single-run callers, and when omitted the
        first row in ``run_id`` order is returned deterministically."""
        if run_id is not None:
            return self._conn.execute(
                "SELECT * FROM coverage_records WHERE run_id = ? AND coverage_id = ?",
                (run_id, coverage_id),
            ).fetchone()
        return self._conn.execute(
            "SELECT * FROM coverage_records WHERE coverage_id = ? ORDER BY run_id LIMIT 1",
            (coverage_id,),
        ).fetchone()

    def list_coverage(self, run_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM coverage_records WHERE run_id = ? ORDER BY coverage_id", (run_id,)
        ).fetchall()

    def coverage_summary(self, run_id: str) -> dict[str, int]:
        """Return {status: count} plus planned/terminal totals for a run."""
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM coverage_records WHERE run_id = ? GROUP BY status",
            (run_id,),
        ).fetchall()
        summary = {row["status"]: int(row["n"]) for row in rows}
        summary["_planned"] = int(
            self._conn.execute(
                "SELECT COUNT(*) AS n FROM coverage_records WHERE run_id = ?", (run_id,)
            ).fetchone()["n"]
        )
        summary["_completed"] = int(
            self._conn.execute(
                "SELECT COUNT(*) AS n FROM coverage_records WHERE run_id = ? AND completed = 1",
                (run_id,),
            ).fetchone()["n"]
        )
        return summary

    # ------------------------------------------------------------------
    # Phase 1A — source health / yield history
    # ------------------------------------------------------------------
    def record_source_health(
        self,
        history_id: str,
        source_instance: str,
        state: str,
        *,
        source_type: Optional[str] = None,
        result_count: Optional[int] = None,
        expected_structure_present: Optional[bool] = None,
        reason: str = "",
        observed_at: Optional[str] = None,
        evidence: Optional[dict] = None,
        query_signature: Optional[str] = None,
        lane: Optional[str] = None,
        geography_group: Optional[str] = None,
        search_mode: Optional[str] = None,
    ) -> bool:
        """Append a source health/yield observation (idempotent on
        ``history_id``). Enough deterministic history to detect a future
        false zero (recent yields 42,38,47 then 0). Phase 1B keys the
        observation by ``query_signature`` so a zero for one lane/geo/mode
        is never compared against unrelated searches (build spec 7.7)."""
        esp = None if expected_structure_present is None else (1 if expected_structure_present else 0)
        with self._auto() as conn:
            cur = self._insert_source_health(
                conn, history_id, source_instance, state, source_type=source_type,
                result_count=result_count, expected_structure_present=esp, reason=reason,
                observed_at=observed_at, evidence=evidence, query_signature=query_signature,
                lane=lane, geography_group=geography_group, search_mode=search_mode,
            )
        return bool(cur.rowcount)

    def _insert_source_health(
        self, conn, history_id, source_instance, state, *, source_type=None, result_count=None,
        expected_structure_present=None, reason="", observed_at=None, evidence=None,
        query_signature=None, lane=None, geography_group=None, search_mode=None,
    ):
        """Conn-scoped health insert shared by the standalone record and the
        atomic fenced page commit. ``expected_structure_present`` is pre-encoded
        (0/1/None) by the caller."""
        return conn.execute(
            "INSERT OR IGNORE INTO source_health_history (history_id, source_instance, source_type, "
            "state, result_count, expected_structure_present, reason, observed_at, evidence_json, "
            "query_signature, lane, geography_group, search_mode) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                history_id, source_instance, source_type, state, result_count,
                expected_structure_present, reason, observed_at or _utcnow(),
                json.dumps(evidence or {}, sort_keys=True),
                query_signature, lane, geography_group, search_mode,
            ),
        )

    def list_source_health(self, source_instance: str, limit: int = 20) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM source_health_history WHERE source_instance = ? "
            "ORDER BY observed_at DESC, history_id DESC LIMIT ?",
            (source_instance, int(limit)),
        ).fetchall()

    def recent_yields(self, source_instance: str, limit: int = 5) -> list[int]:
        """Most-recent non-null result counts (newest first) for false-zero
        detection. NOTE: this is the coarse per-instance view retained for
        backward compatibility; prefer :meth:`recent_yields_for_signature`."""
        rows = self._conn.execute(
            "SELECT result_count FROM source_health_history WHERE source_instance = ? "
            "AND result_count IS NOT NULL ORDER BY observed_at DESC, history_id DESC LIMIT ?",
            (source_instance, int(limit)),
        ).fetchall()
        return [int(r["result_count"]) for r in rows]

    def recent_yields_for_signature(self, query_signature: str, limit: int = 5) -> list[int]:
        """Most-recent non-null yields for one query signature (newest first).
        A zero here is compared only against the SAME lane/geo/mode/keyword
        bundle, preventing a false selector-drift verdict."""
        rows = self._conn.execute(
            "SELECT result_count FROM source_health_history WHERE query_signature = ? "
            "AND result_count IS NOT NULL ORDER BY observed_at DESC, history_id DESC LIMIT ?",
            (query_signature, int(limit)),
        ).fetchall()
        return [int(r["result_count"]) for r in rows]

    # ------------------------------------------------------------------
    # Phase 1B — first-class source instance persistence (build spec 7.4)
    # ------------------------------------------------------------------
    def upsert_source_instance(
        self,
        instance_id: str,
        adapter_key: str,
        source_type: str,
        category: str,
        *,
        display_name: str = "",
        base_url: Optional[str] = None,
        tenant: Optional[str] = None,
        site: Optional[str] = None,
        company_id: Optional[str] = None,
        enabled: bool = True,
        lifecycle_state: str = "ACTIVE",
        capability_additions: Optional[list] = None,
        capability_removals: Optional[list] = None,
        rate_policy_ref: Optional[str] = None,
        auth_ref: Optional[str] = None,
        provenance: Optional[dict] = None,
    ) -> None:
        """Insert or update one normalized source instance (idempotent on
        ``instance_id``). ``auth_ref`` is a reference NAME only — never a
        secret value. Company↔source coverage is connected through these
        stable instance IDs."""
        now = _utcnow()
        with self._auto() as conn:
            conn.execute(
                "INSERT INTO source_instances (instance_id, adapter_key, source_type, category, "
                "display_name, base_url, tenant, site, company_id, enabled, lifecycle_state, "
                "capability_additions_json, capability_removals_json, rate_policy_ref, auth_ref, "
                "provenance_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(instance_id) DO UPDATE SET adapter_key=excluded.adapter_key, "
                "source_type=excluded.source_type, category=excluded.category, "
                "display_name=excluded.display_name, base_url=excluded.base_url, "
                "tenant=excluded.tenant, site=excluded.site, company_id=excluded.company_id, "
                "enabled=excluded.enabled, lifecycle_state=excluded.lifecycle_state, "
                "capability_additions_json=excluded.capability_additions_json, "
                "capability_removals_json=excluded.capability_removals_json, "
                "rate_policy_ref=excluded.rate_policy_ref, auth_ref=excluded.auth_ref, "
                "provenance_json=excluded.provenance_json, updated_at=excluded.updated_at",
                (
                    instance_id, adapter_key, source_type, category, display_name, base_url,
                    tenant, site, company_id, 1 if enabled else 0, lifecycle_state,
                    json.dumps(sorted(capability_additions or []), sort_keys=True),
                    json.dumps(sorted(capability_removals or []), sort_keys=True),
                    rate_policy_ref, auth_ref, json.dumps(provenance or {}, sort_keys=True),
                    now, now,
                ),
            )

    def get_source_instance(self, instance_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM source_instances WHERE instance_id = ?", (instance_id,)
        ).fetchone()

    def list_source_instances(
        self, *, company_id: Optional[str] = None, include_disabled: bool = True
    ) -> list[sqlite3.Row]:
        clauses = []
        params: list = []
        if company_id is not None:
            clauses.append("company_id = ?")
            params.append(company_id)
        if not include_disabled:
            clauses.append("enabled = 1")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return self._conn.execute(
            f"SELECT * FROM source_instances{where} ORDER BY instance_id", tuple(params)
        ).fetchall()

    # ------------------------------------------------------------------
    # Phase 1C-B — official career-site coverage (build spec 12)
    # ------------------------------------------------------------------
    def record_career_entry_point(
        self,
        entry_id: str,
        entry_url: str,
        *,
        company_id: Optional[str] = None,
        label: Optional[str] = None,
        discovery_method: str = "UNKNOWN",
        trusted: bool = False,
        trust_kind: Optional[str] = None,
        confidence: float = 0.0,
        evidence: Optional[dict] = None,
        validated: bool = False,
        reachability: Optional[str] = None,
        observed_at: Optional[str] = None,
    ) -> None:
        """Append-only record of a discovered career entry point (idempotent on
        ``entry_id``). A company may have MANY current entry points (global /
        India / graduate); recording one never overwrites another.

        ``trusted`` means only that the URL SHAPE is an official/known-ATS host;
        ``validated`` means reachability/content/redirect evidence confirmed a
        real entry point. A guessed common path (``/careers``) that no one has
        fetched is trusted-but-not-validated — a LEAD, never a resolved entry
        (build spec 5.3)."""
        now = observed_at or _utcnow()
        with self._auto() as conn:
            conn.execute(
                "INSERT INTO career_entry_points (entry_id, company_id, entry_url, label, "
                "discovery_method, trusted, trust_kind, confidence, evidence_json, validated, "
                "reachability, observed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(entry_id) DO UPDATE SET company_id=excluded.company_id, "
                "entry_url=excluded.entry_url, label=excluded.label, "
                "discovery_method=excluded.discovery_method, trusted=excluded.trusted, "
                "trust_kind=excluded.trust_kind, confidence=excluded.confidence, "
                "evidence_json=excluded.evidence_json, validated=excluded.validated, "
                "reachability=excluded.reachability",
                (
                    entry_id, company_id, entry_url, label, discovery_method,
                    1 if trusted else 0, trust_kind, float(confidence),
                    json.dumps(evidence or {}, sort_keys=True),
                    1 if validated else 0, reachability, now,
                ),
            )

    def list_career_entry_points(self, *, company_id: Optional[str] = None) -> list[sqlite3.Row]:
        if company_id is not None:
            return self._conn.execute(
                "SELECT * FROM career_entry_points WHERE company_id = ? ORDER BY entry_id",
                (company_id,),
            ).fetchall()
        return self._conn.execute(
            "SELECT * FROM career_entry_points ORDER BY entry_id"
        ).fetchall()

    def upsert_career_profile(
        self,
        profile_id: str,
        *,
        source_instance_id: str,
        entry_url: str,
        route_kind: str,
        company_id: Optional[str] = None,
        recipe: Optional[dict] = None,
        confidence: float = 0.0,
        evidence: Optional[dict] = None,
        recipe_version: str = "1.0.0",
        parser_version: str = "",
        health: str = "UNVALIDATED",
        last_success_at: Optional[str] = None,
        last_validated_at: Optional[str] = None,
    ) -> None:
        """Persist a generic career-site profile/recipe (idempotent on
        ``source_instance_id`` — one current profile per company-source
        instance). The recipe is DATA (route kind, selectors, link patterns),
        never executable code."""
        now = _utcnow()
        with self._auto() as conn:
            conn.execute(
                "INSERT INTO career_site_profiles (source_instance_id, profile_id, company_id, "
                "entry_url, route_kind, recipe_json, confidence, evidence_json, recipe_version, "
                "parser_version, health, last_success_at, last_validated_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(source_instance_id) DO UPDATE SET profile_id=excluded.profile_id, "
                "company_id=excluded.company_id, entry_url=excluded.entry_url, "
                "route_kind=excluded.route_kind, recipe_json=excluded.recipe_json, "
                "confidence=excluded.confidence, evidence_json=excluded.evidence_json, "
                "recipe_version=excluded.recipe_version, parser_version=excluded.parser_version, "
                "health=excluded.health, last_success_at=excluded.last_success_at, "
                "last_validated_at=excluded.last_validated_at, updated_at=excluded.updated_at",
                (
                    source_instance_id, profile_id, company_id, entry_url, route_kind,
                    json.dumps(recipe or {}, sort_keys=True), float(confidence),
                    json.dumps(evidence or {}, sort_keys=True), recipe_version, parser_version,
                    health, last_success_at, last_validated_at, now, now,
                ),
            )

    def get_career_profile(self, source_instance_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM career_site_profiles WHERE source_instance_id = ?",
            (source_instance_id,),
        ).fetchone()

    def list_career_profiles(self, *, company_id: Optional[str] = None) -> list[sqlite3.Row]:
        if company_id is not None:
            return self._conn.execute(
                "SELECT * FROM career_site_profiles WHERE company_id = ? ORDER BY source_instance_id",
                (company_id,),
            ).fetchall()
        return self._conn.execute(
            "SELECT * FROM career_site_profiles ORDER BY source_instance_id"
        ).fetchall()

    def record_route_classification(
        self,
        classification_id: str,
        entry_url: str,
        route_kind: str,
        *,
        company_id: Optional[str] = None,
        source_instance_id: Optional[str] = None,
        fingerprint_family: Optional[str] = None,
        confidence: float = 0.0,
        evidence: Optional[dict] = None,
        classified_at: Optional[str] = None,
    ) -> None:
        """Append-only route classification for one entry point (idempotent on
        ``classification_id``)."""
        now = classified_at or _utcnow()
        with self._auto() as conn:
            conn.execute(
                "INSERT INTO career_route_classifications (classification_id, company_id, "
                "source_instance_id, entry_url, route_kind, fingerprint_family, confidence, "
                "evidence_json, classified_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(classification_id) DO UPDATE SET route_kind=excluded.route_kind, "
                "fingerprint_family=excluded.fingerprint_family, confidence=excluded.confidence, "
                "evidence_json=excluded.evidence_json",
                (
                    classification_id, company_id, source_instance_id, entry_url, route_kind,
                    fingerprint_family, float(confidence), json.dumps(evidence or {}, sort_keys=True), now,
                ),
            )

    def list_route_classifications(self, *, source_instance_id: Optional[str] = None) -> list[sqlite3.Row]:
        if source_instance_id is not None:
            return self._conn.execute(
                "SELECT * FROM career_route_classifications WHERE source_instance_id = ? ORDER BY classified_at",
                (source_instance_id,),
            ).fetchall()
        return self._conn.execute(
            "SELECT * FROM career_route_classifications ORDER BY classified_at"
        ).fetchall()

    def upsert_career_pilot_run(
        self,
        run_id: str,
        config_hash: str,
        *,
        config: Optional[dict] = None,
        status: str = "SEALED",
        results: Optional[dict] = None,
    ) -> None:
        """Persist the sealed pilot config (hash + JSON) and its results summary.
        The config is sealed BEFORE execution; a re-save updates only status/
        results, never the sealed config hash."""
        now = _utcnow()
        with self._auto() as conn:
            conn.execute(
                "INSERT INTO career_pilot_runs (run_id, config_hash, config_json, status, "
                "results_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET status=excluded.status, "
                "results_json=excluded.results_json, updated_at=excluded.updated_at",
                (
                    run_id, config_hash, json.dumps(config or {}, sort_keys=True), status,
                    json.dumps(results or {}, sort_keys=True), now, now,
                ),
            )

    def get_career_pilot_run(self, run_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM career_pilot_runs WHERE run_id = ?", (run_id,)
        ).fetchone()

    # ================================================================
    # Phase 1D — market discovery / portals / campaigns / waves
    # ================================================================
    def record_portal_lead(
        self,
        lead_id: str,
        run_id: str,
        source_family: str,
        *,
        campaign_id: Optional[str] = None,
        wave_id: Optional[str] = None,
        source_instance: Optional[str] = None,
        portal_job_id: Optional[str] = None,
        canonical_url: Optional[str] = None,
        title: Optional[str] = None,
        company_name: Optional[str] = None,
        company_id: Optional[str] = None,
        location: Optional[str] = None,
        posted_text: Optional[str] = None,
        posted_provenance: Optional[str] = None,
        work_mode: Optional[str] = None,
        lane: Optional[str] = None,
        result_query: Optional[str] = None,
        result_page: Optional[int] = None,
        result_cursor: Optional[str] = None,
        salary_text: Optional[str] = None,
        experience_text: Optional[str] = None,
        official_apply_url: Optional[str] = None,
        company_url: Optional[str] = None,
        verification_state: str = "PORTAL_CURRENT_LEAD",
        raw_evidence_ref: Optional[str] = None,
        health_findings: Optional[list] = None,
        detail: Optional[dict] = None,
        content_hash: Optional[str] = None,
        observed_at: Optional[str] = None,
    ) -> None:
        """Append-only portal job lead (idempotent on ``lead_id``). A portal lead
        is NEVER an official-verified job; its ``verification_state`` starts at
        PORTAL_CURRENT_LEAD (build spec 6). Later columns (company_id / official
        apply URL / verification_state) may be enriched by follow-up without ever
        deleting the original observation."""
        now = observed_at or _utcnow()
        with self._auto() as conn:
            conn.execute(
                "INSERT INTO portal_leads (lead_id, run_id, campaign_id, wave_id, source_family, "
                "source_instance, portal_job_id, canonical_url, title, company_name, company_id, "
                "location, posted_text, posted_provenance, work_mode, lane, result_query, "
                "result_page, result_cursor, salary_text, experience_text, official_apply_url, "
                "company_url, verification_state, raw_evidence_ref, health_findings_json, "
                "detail_json, content_hash, observed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(lead_id) DO UPDATE SET company_id=excluded.company_id, "
                "official_apply_url=excluded.official_apply_url, company_url=excluded.company_url, "
                "verification_state=excluded.verification_state, detail_json=excluded.detail_json",
                (
                    lead_id, run_id, campaign_id, wave_id, source_family, source_instance,
                    portal_job_id, canonical_url, title, company_name, company_id, location,
                    posted_text, posted_provenance, work_mode, lane, result_query, result_page,
                    result_cursor, salary_text, experience_text, official_apply_url, company_url,
                    verification_state, raw_evidence_ref,
                    json.dumps(health_findings or [], sort_keys=True),
                    json.dumps(detail or {}, sort_keys=True), content_hash, now,
                ),
            )

    def list_portal_leads(
        self, run_id: str, *, source_family: Optional[str] = None,
        verification_state: Optional[str] = None,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM portal_leads WHERE run_id = ?"
        params: list = [run_id]
        if source_family is not None:
            sql += " AND source_family = ?"
            params.append(source_family)
        if verification_state is not None:
            sql += " AND verification_state = ?"
            params.append(verification_state)
        sql += " ORDER BY lead_id"
        return self._conn.execute(sql, tuple(params)).fetchall()

    def get_portal_lead(self, lead_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM portal_leads WHERE lead_id = ?", (lead_id,)
        ).fetchone()

    def set_portal_lead_company(self, lead_id: str, company_id: Optional[str]) -> None:
        with self._auto() as conn:
            conn.execute(
                "UPDATE portal_leads SET company_id = ? WHERE lead_id = ?", (company_id, lead_id)
            )

    def set_portal_lead_verification(self, lead_id: str, verification_state: str) -> None:
        with self._auto() as conn:
            conn.execute(
                "UPDATE portal_leads SET verification_state = ? WHERE lead_id = ?",
                (verification_state, lead_id),
            )

    # -- market campaigns --------------------------------------------------
    def upsert_market_campaign(
        self,
        campaign_id: str,
        run_id: str,
        *,
        policy_fingerprint: Optional[str] = None,
        candidate_fingerprint: Optional[str] = None,
        mode: str = "DELTA",
        budgets: Optional[dict] = None,
        status: str = "SEALED",
        current_wave: int = 0,
        terminal_reason: Optional[str] = None,
        config: Optional[dict] = None,
    ) -> None:
        now = _utcnow()
        with self._auto() as conn:
            conn.execute(
                "INSERT INTO market_campaigns (campaign_id, run_id, policy_fingerprint, "
                "candidate_fingerprint, mode, budgets_json, status, current_wave, terminal_reason, "
                "config_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(campaign_id) DO UPDATE SET status=excluded.status, "
                "current_wave=excluded.current_wave, terminal_reason=excluded.terminal_reason, "
                "budgets_json=excluded.budgets_json, updated_at=excluded.updated_at",
                (
                    campaign_id, run_id, policy_fingerprint, candidate_fingerprint, mode,
                    json.dumps(budgets or {}, sort_keys=True), status, int(current_wave),
                    terminal_reason, json.dumps(config or {}, sort_keys=True), now, now,
                ),
            )

    def get_market_campaign(self, campaign_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM market_campaigns WHERE campaign_id = ?", (campaign_id,)
        ).fetchone()

    def get_market_campaign_for_run(self, run_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM market_campaigns WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
            (run_id,),
        ).fetchone()

    # -- market waves (append-only, sealed) --------------------------------
    def upsert_market_wave(
        self,
        wave_id: str,
        campaign_id: str,
        run_id: str,
        wave_index: int,
        *,
        parent_wave_id: Optional[str] = None,
        seal_hash: Optional[str] = None,
        status: str = "PLANNED",
        deficit_reason: Optional[str] = None,
        tasks: Optional[list] = None,
        terminal_reason: Optional[str] = None,
        sealed_at: Optional[str] = None,
    ) -> None:
        """Idempotent on ``wave_id``. A SEALED wave's identity fields (index/
        seal_hash/tasks) must never be mutated — only its status/terminal_reason
        advance (build spec 10)."""
        now = _utcnow()
        with self._auto() as conn:
            conn.execute(
                "INSERT INTO market_waves (wave_id, campaign_id, run_id, wave_index, parent_wave_id, "
                "seal_hash, status, deficit_reason, tasks_json, terminal_reason, created_at, "
                "sealed_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(wave_id) DO UPDATE SET status=excluded.status, "
                "terminal_reason=excluded.terminal_reason, sealed_at=COALESCE(market_waves.sealed_at, "
                "excluded.sealed_at), updated_at=excluded.updated_at",
                (
                    wave_id, campaign_id, run_id, int(wave_index), parent_wave_id, seal_hash,
                    status, deficit_reason, json.dumps(tasks or [], sort_keys=True),
                    terminal_reason, now, sealed_at, now,
                ),
            )

    def get_market_wave(self, wave_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM market_waves WHERE wave_id = ?", (wave_id,)
        ).fetchone()

    def list_market_waves(self, campaign_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM market_waves WHERE campaign_id = ? ORDER BY wave_index",
            (campaign_id,),
        ).fetchall()

    def record_wave_deficit(
        self,
        deficit_id: str,
        campaign_id: str,
        wave_id: str,
        deficit_kind: str,
        *,
        lane: Optional[str] = None,
        geography: Optional[str] = None,
        source_family: Optional[str] = None,
        detail: Optional[dict] = None,
        created_at: Optional[str] = None,
    ) -> None:
        now = created_at or _utcnow()
        with self._auto() as conn:
            conn.execute(
                "INSERT INTO wave_deficits (deficit_id, campaign_id, wave_id, deficit_kind, lane, "
                "geography, source_family, detail_json, created_at) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(deficit_id) DO UPDATE SET detail_json=excluded.detail_json",
                (
                    deficit_id, campaign_id, wave_id, deficit_kind, lane, geography,
                    source_family, json.dumps(detail or {}, sort_keys=True), now,
                ),
            )

    def list_wave_deficits(self, campaign_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM wave_deficits WHERE campaign_id = ? ORDER BY created_at",
            (campaign_id,),
        ).fetchall()

    def record_query_variant(
        self,
        variant_id: str,
        campaign_id: str,
        query: str,
        *,
        wave_id: Optional[str] = None,
        lane: Optional[str] = None,
        geography: Optional[str] = None,
        source_family: Optional[str] = None,
        origin: str = "SYNONYM",
        policy_version: Optional[str] = None,
        reason: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> None:
        now = created_at or _utcnow()
        with self._auto() as conn:
            conn.execute(
                "INSERT INTO query_variants (variant_id, campaign_id, wave_id, lane, geography, "
                "source_family, query, origin, policy_version, reason, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(variant_id) DO NOTHING",
                (
                    variant_id, campaign_id, wave_id, lane, geography, source_family, query,
                    origin, policy_version, reason, now,
                ),
            )

    def list_query_variants(self, campaign_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM query_variants WHERE campaign_id = ? ORDER BY created_at",
            (campaign_id,),
        ).fetchall()

    # -- dynamic company provenance ----------------------------------------
    def record_dynamic_company_provenance(
        self,
        provenance_id: str,
        discovery_source: str,
        *,
        company_id: Optional[str] = None,
        run_id: Optional[str] = None,
        portal_lead_id: Optional[str] = None,
        normalized_name: Optional[str] = None,
        resolved_domain: Optional[str] = None,
        resolution_status: str = "DYNAMICALLY_DISCOVERED",
        evidence: Optional[dict] = None,
        created_at: Optional[str] = None,
    ) -> None:
        now = created_at or _utcnow()
        with self._auto() as conn:
            conn.execute(
                "INSERT INTO dynamic_company_provenance (provenance_id, company_id, run_id, "
                "discovery_source, portal_lead_id, normalized_name, resolved_domain, "
                "resolution_status, evidence_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(provenance_id) DO UPDATE SET company_id=excluded.company_id, "
                "resolved_domain=excluded.resolved_domain, resolution_status=excluded.resolution_status, "
                "evidence_json=excluded.evidence_json",
                (
                    provenance_id, company_id, run_id, discovery_source, portal_lead_id,
                    normalized_name, resolved_domain, resolution_status,
                    json.dumps(evidence or {}, sort_keys=True), now,
                ),
            )

    def list_dynamic_company_provenance(
        self, *, resolution_status: Optional[str] = None, run_id: Optional[str] = None,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM dynamic_company_provenance WHERE 1=1"
        params: list = []
        if resolution_status is not None:
            sql += " AND resolution_status = ?"
            params.append(resolution_status)
        if run_id is not None:
            sql += " AND run_id = ?"
            params.append(run_id)
        sql += " ORDER BY created_at"
        return self._conn.execute(sql, tuple(params)).fetchall()

    # -- portal -> official links ------------------------------------------
    def record_portal_official_link(
        self,
        link_id: str,
        run_id: str,
        portal_lead_id: str,
        *,
        canonical_id: Optional[str] = None,
        official_observation_id: Optional[str] = None,
        match_kind: str = "NONE",
        confidence: float = 0.0,
        verification_state: str = "PORTAL_CURRENT_LEAD",
        ambiguous: bool = False,
        evidence: Optional[dict] = None,
        created_at: Optional[str] = None,
    ) -> None:
        now = created_at or _utcnow()
        with self._auto() as conn:
            conn.execute(
                "INSERT INTO portal_official_links (link_id, run_id, portal_lead_id, canonical_id, "
                "official_observation_id, match_kind, confidence, verification_state, ambiguous, "
                "evidence_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(link_id) DO UPDATE SET canonical_id=excluded.canonical_id, "
                "official_observation_id=excluded.official_observation_id, match_kind=excluded.match_kind, "
                "confidence=excluded.confidence, verification_state=excluded.verification_state, "
                "ambiguous=excluded.ambiguous, evidence_json=excluded.evidence_json",
                (
                    link_id, run_id, portal_lead_id, canonical_id, official_observation_id,
                    match_kind, float(confidence), verification_state, 1 if ambiguous else 0,
                    json.dumps(evidence or {}, sort_keys=True), now,
                ),
            )

    def list_portal_official_links(self, run_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM portal_official_links WHERE run_id = ? ORDER BY link_id",
            (run_id,),
        ).fetchall()

    # -- global campaign budget (run-scoped) -------------------------------
    def init_campaign_budget(
        self,
        campaign_id: str,
        run_id: str,
        *,
        max_http: Optional[int] = None,
        max_browser: Optional[int] = None,
        max_llm: Optional[int] = None,
        max_results: Optional[int] = None,
        max_wall_clock_s: Optional[float] = None,
        started_at: Optional[str] = None,
    ) -> None:
        now = _utcnow()
        with self._auto() as conn:
            conn.execute(
                "INSERT INTO campaign_budget (campaign_id, run_id, max_http, max_browser, max_llm, "
                "max_results, max_wall_clock_s, started_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(campaign_id) DO UPDATE SET max_http=excluded.max_http, "
                "max_browser=excluded.max_browser, max_llm=excluded.max_llm, "
                "max_results=excluded.max_results, max_wall_clock_s=excluded.max_wall_clock_s",
                (
                    campaign_id, run_id, max_http, max_browser, max_llm, max_results,
                    max_wall_clock_s, started_at or now, now,
                ),
            )

    def bump_campaign_budget(
        self, campaign_id: str, *, http: int = 0, browser: int = 0, llm: int = 0, results: int = 0,
    ) -> sqlite3.Row:
        """Atomically add to the run-scoped campaign budget counters and return
        the updated row. The caller compares against the max_* ceilings to decide
        PARTIAL_BUDGET (build spec 9)."""
        now = _utcnow()
        with self._auto() as conn:
            conn.execute(
                "UPDATE campaign_budget SET http_calls = http_calls + ?, browser_calls = browser_calls + ?, "
                "llm_calls = llm_calls + ?, results = results + ?, updated_at = ? WHERE campaign_id = ?",
                (int(http), int(browser), int(llm), int(results), now, campaign_id),
            )
        return self.get_campaign_budget(campaign_id)

    def reserve_campaign_budget(
        self, campaign_id: str, *, http: int = 0, browser: int = 0, llm: int = 0,
    ) -> bool:
        """Atomically RESERVE budget for one call-class only when the reservation
        stays within the configured ceiling (build spec 9). Returns True when the
        reservation succeeded (the counter was advanced), False when the ceiling
        would be exceeded (nothing advanced) — so the caller records PARTIAL_BUDGET.

        The conditional UPDATE is serialized by SQLite's single-writer lock, so
        concurrent workers can never collectively exceed the cap (the check and
        the increment are one atomic statement, not a racy read-then-write)."""
        now = _utcnow()
        col = "browser_calls" if browser else "http_calls" if http else "llm_calls"
        maxcol = "max_browser" if browser else "max_http" if http else "max_llm"
        amount = int(browser or http or llm or 0)
        if amount <= 0:
            return True
        with self._auto() as conn:
            cur = conn.execute(
                f"UPDATE campaign_budget SET {col} = {col} + ?, updated_at = ? "
                f"WHERE campaign_id = ? AND ({maxcol} IS NULL OR {col} + ? <= {maxcol})",
                (amount, now, campaign_id, amount),
            )
            return cur.rowcount > 0

    def get_campaign_budget(self, campaign_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM campaign_budget WHERE campaign_id = ?", (campaign_id,)
        ).fetchone()

    def append_coverage_attempt(
        self,
        attempt_id: str,
        coverage_id: str,
        run_id: str,
        source_instance: str,
        status: str,
        *,
        query_signature: Optional[str] = None,
        jobs_found: int = 0,
        new_jobs: int = 0,
        changed_jobs: int = 0,
        closed_jobs: int = 0,
        detail: Optional[dict] = None,
        observed_at: Optional[str] = None,
    ) -> bool:
        """Append an immutable coverage attempt (idempotent on ``attempt_id``,
        never updated). Keeps a full diagnostic trail even though the resolved
        ``coverage_records`` row is upserted."""
        with self._auto() as conn:
            cur = self._insert_coverage_attempt(
                conn, attempt_id, coverage_id, run_id, source_instance, status,
                query_signature=query_signature, jobs_found=jobs_found, new_jobs=new_jobs,
                changed_jobs=changed_jobs, closed_jobs=closed_jobs, detail=detail, observed_at=observed_at,
            )
        return bool(cur.rowcount)

    def _insert_coverage_attempt(
        self, conn, attempt_id, coverage_id, run_id, source_instance, status, *,
        query_signature=None, jobs_found=0, new_jobs=0, changed_jobs=0, closed_jobs=0,
        detail=None, observed_at=None,
    ):
        """Conn-scoped attempt insert shared by the standalone append and the
        atomic fenced page commit (so both write the exact same row)."""
        return conn.execute(
            "INSERT OR IGNORE INTO coverage_attempts (attempt_id, coverage_id, run_id, "
            "source_instance, query_signature, status, jobs_found, new_jobs, changed_jobs, "
            "closed_jobs, detail_json, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                attempt_id, coverage_id, run_id, source_instance, query_signature, status,
                int(jobs_found), int(new_jobs), int(changed_jobs), int(closed_jobs),
                json.dumps(detail or {}, sort_keys=True), observed_at or _utcnow(),
            ),
        )

    def list_coverage_attempts(self, coverage_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM coverage_attempts WHERE coverage_id = ? ORDER BY observed_at, attempt_id",
            (coverage_id,),
        ).fetchall()

    # ------------------------------------------------------------------
    # Phase 1B.1 — raw discovery observation staging (build spec 12 / P0-15)
    # ------------------------------------------------------------------
    def stage_raw_observation(
        self,
        observation_id: str,
        run_id: str,
        source_instance: str,
        content_hash: str,
        *,
        coverage_id: Optional[str] = None,
        attempt_id: Optional[str] = None,
        query_signature: Optional[str] = None,
        source_family: Optional[str] = None,
        source_job_id: Optional[str] = None,
        source_url: Optional[str] = None,
        canonical_url: Optional[str] = None,
        company: Optional[str] = None,
        title: Optional[str] = None,
        location: Optional[str] = None,
        lane: Optional[str] = None,
        posted_at: Optional[str] = None,
        is_active: Optional[str] = None,
        source_identity: Optional[str] = None,
        raw_evidence_ref: Optional[str] = None,
        adapter_version: str = "",
        parser_version: str = "",
        observed_at: Optional[str] = None,
        detail: Optional[dict] = None,
        parent_observation_id: Optional[str] = None,
        revision_kind: str = "SEARCH",
        processing_status: str = "STAGED",
    ) -> bool:
        """Append a raw normalized source observation to the staging table
        (idempotent on ``observation_id``, NEVER updated once staged). This is
        written during DISCOVER; canonicalization happens later in DEDUPE, so
        raw discoveries never become canonical jobs directly (P0-15). Returns
        True if a new row was inserted.

        ``parent_observation_id`` links a DETAIL revision to its source SEARCH
        observation (build spec 6); ``revision_kind`` is 'SEARCH' for an original
        discovery or 'DETAIL' for a hydrated revision. ``canonical_id`` is NEVER
        used to hold a parent observation id."""
        with self._auto() as conn:
            cur = self._insert_raw_observation(
                conn, observation_id, run_id, source_instance, content_hash,
                coverage_id=coverage_id, attempt_id=attempt_id, query_signature=query_signature,
                source_family=source_family, source_job_id=source_job_id, source_url=source_url,
                canonical_url=canonical_url, company=company, title=title, location=location,
                lane=lane, posted_at=posted_at, is_active=is_active, source_identity=source_identity,
                raw_evidence_ref=raw_evidence_ref, adapter_version=adapter_version,
                parser_version=parser_version, observed_at=observed_at, detail=detail,
                parent_observation_id=parent_observation_id, revision_kind=revision_kind,
                processing_status=processing_status,
            )
        return bool(cur.rowcount)

    def _insert_raw_observation(
        self, conn, observation_id, run_id, source_instance, content_hash, *,
        coverage_id=None, attempt_id=None, query_signature=None, source_family=None,
        source_job_id=None, source_url=None, canonical_url=None, company=None, title=None,
        location=None, lane=None, posted_at=None, is_active=None, source_identity=None,
        raw_evidence_ref=None, adapter_version="", parser_version="", observed_at=None,
        detail=None, parent_observation_id=None, revision_kind="SEARCH", processing_status="STAGED",
    ):
        """Conn-scoped observation insert shared by the standalone staging call
        and the atomic fenced page commit."""
        return conn.execute(
            "INSERT OR IGNORE INTO raw_discovery_observations (observation_id, run_id, coverage_id, "
            "attempt_id, query_signature, source_instance, source_family, source_job_id, source_url, "
            "canonical_url, company, title, location, lane, posted_at, is_active, content_hash, "
            "source_identity, raw_evidence_ref, adapter_version, parser_version, processing_status, "
            "canonical_id, observed_at, detail_json, parent_observation_id, revision_kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
            (
                observation_id, run_id, coverage_id, attempt_id, query_signature, source_instance,
                source_family, source_job_id, source_url, canonical_url, company, title, location,
                lane, posted_at, is_active, content_hash, source_identity, raw_evidence_ref,
                adapter_version, parser_version, processing_status, observed_at or _utcnow(),
                json.dumps(detail or {}, sort_keys=True), parent_observation_id, revision_kind,
            ),
        )

    def list_raw_observations(
        self, run_id: str, *, processing_status: Optional[str] = None
    ) -> list[sqlite3.Row]:
        if processing_status is not None:
            return self._conn.execute(
                "SELECT * FROM raw_discovery_observations WHERE run_id = ? AND processing_status = ? "
                "ORDER BY observed_at, observation_id",
                (run_id, processing_status),
            ).fetchall()
        return self._conn.execute(
            "SELECT * FROM raw_discovery_observations WHERE run_id = ? ORDER BY observed_at, observation_id",
            (run_id,),
        ).fetchall()

    def count_raw_observations(self, run_id: Optional[str] = None) -> int:
        if run_id is None:
            return int(
                self._conn.execute("SELECT COUNT(*) AS n FROM raw_discovery_observations").fetchone()["n"]
            )
        return int(
            self._conn.execute(
                "SELECT COUNT(*) AS n FROM raw_discovery_observations WHERE run_id = ?", (run_id,)
            ).fetchone()["n"]
        )

    def get_raw_observation(self, observation_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM raw_discovery_observations WHERE observation_id = ?", (observation_id,)
        ).fetchone()

    def mark_raw_observation_processed(
        self, observation_id: str, canonical_id: str, *, processing_status: str = "CANONICALIZED"
    ) -> None:
        with self._auto() as conn:
            conn.execute(
                "UPDATE raw_discovery_observations SET processing_status=?, canonical_id=? "
                "WHERE observation_id=?",
                (processing_status, canonical_id, observation_id),
            )

    # ------------------------------------------------------------------
    # Phase 1C-A — atomic coverage task leases (build spec 7)
    # ------------------------------------------------------------------
    @contextlib.contextmanager
    def immediate_transaction(self) -> Iterator[sqlite3.Connection]:
        """A transaction that takes a RESERVED write lock up front via
        ``BEGIN IMMEDIATE``. This makes a read-decide-write lease claim atomic
        against other writers (on separate connections/processes): a second
        writer blocks on the reserved lock — up to the busy timeout — instead of
        racing, so two workers can never both observe the same lease as free."""
        conn = self._conn
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()

    def _append_lease_event(
        self,
        conn: sqlite3.Connection,
        coverage_id: str,
        run_id: str,
        event: str,
        *,
        worker_id: Optional[str] = None,
        attempt: Optional[int] = None,
        lease_expires_at: Optional[str] = None,
        at: str,
        detail: Optional[dict] = None,
    ) -> None:
        event_id = f"lev::{run_id}::{coverage_id}::{event}::{attempt}::{at}"
        conn.execute(
            "INSERT OR IGNORE INTO coverage_lease_events (event_id, coverage_id, run_id, worker_id, "
            "event, attempt, lease_expires_at, at, detail_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, coverage_id, run_id, worker_id, event, attempt, lease_expires_at, at,
             json.dumps(detail or {}, sort_keys=True)),
        )

    def ensure_coverage_lease(
        self,
        coverage_id: str,
        run_id: str,
        *,
        company: Optional[str] = None,
        source_instance: Optional[str] = None,
        tenant: Optional[str] = None,
        terminal: bool = False,
        now: Optional[str] = None,
    ) -> bool:
        """Create the lease row for a coverage child if absent (idempotent).
        An already-terminal child is recorded ``DONE``/terminal so it can never
        be leased. Returns True if a new row was inserted."""
        at = now or _utcnow()
        status = "DONE" if terminal else "AVAILABLE"
        with self._auto() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO coverage_leases (coverage_id, run_id, worker_id, status, terminal, "
                "attempt, version, company, source_instance, tenant, created_at, updated_at) "
                "VALUES (?, ?, NULL, ?, ?, 0, 0, ?, ?, ?, ?, ?)",
                (coverage_id, run_id, status, 1 if terminal else 0, company, source_instance, tenant, at, at),
            )
        return bool(cur.rowcount)

    def acquire_coverage_lease(
        self,
        coverage_id: str,
        run_id: str,
        worker_id: str,
        *,
        now: str,
        lease_expires_at: str,
    ) -> Optional[dict]:
        """Atomically claim a specific coverage child. Returns a lease dict on
        success, or ``None`` when the child is terminal, missing, or already
        held by an unexpired lease. ``now``/``lease_expires_at`` are fixed-width
        UTC ISO strings from the caller's (injectable) clock."""
        with self.immediate_transaction() as conn:
            row = conn.execute(
                "SELECT status, terminal, attempt, version, lease_expires_at FROM coverage_leases "
                "WHERE coverage_id = ? AND run_id = ?",
                (coverage_id, run_id),
            ).fetchone()
            if row is None or int(row["terminal"]) == 1:
                return None
            held = row["status"] == "LEASED" and row["lease_expires_at"] is not None and row["lease_expires_at"] > now
            if held:
                return None
            attempt = int(row["attempt"]) + 1
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE coverage_leases SET status='LEASED', worker_id=?, attempt=?, version=?, "
                "lease_acquired_at=?, lease_expires_at=?, heartbeat_at=?, updated_at=? "
                "WHERE run_id=? AND coverage_id=?",
                (worker_id, attempt, version, now, lease_expires_at, now, now, run_id, coverage_id),
            )
            self._append_lease_event(
                conn, coverage_id, run_id, "ACQUIRE", worker_id=worker_id, attempt=attempt,
                lease_expires_at=lease_expires_at, at=now,
                detail={"reclaimed": row["status"] == "LEASED"},
            )
            return {"coverage_id": coverage_id, "run_id": run_id, "worker_id": worker_id,
                    "attempt": attempt, "version": version, "lease_expires_at": lease_expires_at}

    def acquire_next_coverage_lease(
        self,
        run_id: str,
        worker_id: str,
        *,
        now: str,
        lease_expires_at: str,
        eligible: Optional[list[str]] = None,
    ) -> Optional[dict]:
        """Atomically claim the next available (or expired-and-reclaimable),
        non-terminal coverage child for ``run_id``, in deterministic
        ``coverage_id`` order. ``eligible`` optionally restricts the candidate
        set (e.g. children whose company/instance/tenant is under its
        concurrency cap). Returns a lease dict or ``None`` when none claimable."""
        with self.immediate_transaction() as conn:
            rows = conn.execute(
                "SELECT coverage_id, attempt, version FROM coverage_leases WHERE run_id=? AND terminal=0 "
                "AND (status='AVAILABLE' OR (status='LEASED' AND lease_expires_at <= ?)) ORDER BY coverage_id",
                (run_id, now),
            ).fetchall()
            eligible_set = set(eligible) if eligible is not None else None
            chosen = None
            for r in rows:
                if eligible_set is None or r["coverage_id"] in eligible_set:
                    chosen = r
                    break
            if chosen is None:
                return None
            attempt = int(chosen["attempt"]) + 1
            version = int(chosen["version"]) + 1
            conn.execute(
                "UPDATE coverage_leases SET status='LEASED', worker_id=?, attempt=?, version=?, "
                "lease_acquired_at=?, lease_expires_at=?, heartbeat_at=?, updated_at=? "
                "WHERE run_id=? AND coverage_id=?",
                (worker_id, attempt, version, now, lease_expires_at, now, now, run_id, chosen["coverage_id"]),
            )
            self._append_lease_event(
                conn, chosen["coverage_id"], run_id, "ACQUIRE", worker_id=worker_id, attempt=attempt,
                lease_expires_at=lease_expires_at, at=now,
            )
            return {"coverage_id": chosen["coverage_id"], "run_id": run_id, "worker_id": worker_id,
                    "attempt": attempt, "version": version, "lease_expires_at": lease_expires_at}

    def heartbeat_coverage_lease(
        self, coverage_id: str, run_id: str, worker_id: str, *, version: Optional[int] = None,
        now: str, lease_expires_at: str,
    ) -> "LeaseMutationResult":
        """Extend a held lease's expiry (a long task proving it is still alive).
        FENCED: succeeds only when ``worker_id`` (and, when supplied, the
        ``version`` fencing token) still own a non-terminal, unexpired LEASED
        row. A lease reclaimed by another worker (higher version / different
        owner) or already expired cannot be heartbeat-extended — that attempt is
        refused and audited as a stale rejection so a zombie worker can never
        silently keep a reclaimed lease alive. ``version=None`` fences on the
        owner only (legacy callers)."""
        with self.immediate_transaction() as conn:
            row = conn.execute(
                "SELECT status, terminal, worker_id, version, lease_expires_at FROM coverage_leases "
                "WHERE run_id=? AND coverage_id=?",
                (run_id, coverage_id),
            ).fetchone()
            if row is None:
                return LeaseMutationResult.NOT_FOUND
            version_ok = version is None or int(row["version"]) == int(version)
            owns = (
                row["worker_id"] == worker_id
                and version_ok
                and row["status"] == "LEASED"
                and int(row["terminal"]) == 0
            )
            unexpired = row["lease_expires_at"] is None or row["lease_expires_at"] > now
            if owns and unexpired:
                conn.execute(
                    "UPDATE coverage_leases SET lease_expires_at=?, heartbeat_at=?, updated_at=? "
                    "WHERE run_id=? AND coverage_id=? AND worker_id=? AND status='LEASED' AND terminal=0",
                    (lease_expires_at, now, now, run_id, coverage_id, worker_id),
                )
                self._append_lease_event(
                    conn, coverage_id, run_id, "HEARTBEAT", worker_id=worker_id,
                    lease_expires_at=lease_expires_at, at=now,
                )
                return LeaseMutationResult.APPLIED
            self._append_lease_event(
                conn, coverage_id, run_id, "HEARTBEAT_REJECTED", worker_id=worker_id, at=now,
                detail={"reason": "stale_token", "current_worker": row["worker_id"],
                        "current_version": int(row["version"]), "expired": not unexpired},
            )
            return LeaseMutationResult.STALE_TOKEN_REJECTED

    def complete_coverage_lease(
        self, coverage_id: str, run_id: str, *, worker_id: Optional[str] = None,
        version: Optional[int] = None, now: str, terminal_status: str = "DONE",
    ) -> "LeaseMutationResult":
        """Mark a coverage child's lease terminal so it can never be leased
        again. When ``worker_id``+``version`` are supplied this is FENCED: only
        the current owner+token may complete an active lease; a stale owner is
        refused (STALE_TOKEN_REJECTED, audited) and can never change a terminal
        status; the same owner+token completing twice is a harmless idempotent
        duplicate. Passing no token performs an administrative completion (e.g.
        give-up / no-adapter) that never downgrades an already-terminal row."""
        with self.immediate_transaction() as conn:
            row = conn.execute(
                "SELECT status, terminal, worker_id, version FROM coverage_leases "
                "WHERE run_id=? AND coverage_id=?",
                (run_id, coverage_id),
            ).fetchone()
            if row is None:
                return LeaseMutationResult.NOT_FOUND
            if worker_id is not None and version is not None:
                same_owner = row["worker_id"] == worker_id and int(row["version"]) == int(version)
                if int(row["terminal"]) == 0 and same_owner and row["status"] == "LEASED":
                    conn.execute(
                        "UPDATE coverage_leases SET status=?, terminal=1, heartbeat_at=?, updated_at=? "
                        "WHERE run_id=? AND coverage_id=? AND worker_id=? AND version=? AND status='LEASED' AND terminal=0",
                        (terminal_status, now, now, run_id, coverage_id, worker_id, version),
                    )
                    self._append_lease_event(
                        conn, coverage_id, run_id, "COMPLETE", worker_id=worker_id, at=now,
                        detail={"terminal_status": terminal_status},
                    )
                    return LeaseMutationResult.APPLIED
                if int(row["terminal"]) == 1 and same_owner:
                    return LeaseMutationResult.ALREADY_APPLIED_IDEMPOTENTLY
                self._append_lease_event(
                    conn, coverage_id, run_id, "COMPLETE_REJECTED", worker_id=worker_id, at=now,
                    detail={"reason": "stale_token", "current_worker": row["worker_id"],
                            "current_version": int(row["version"]), "terminal": int(row["terminal"])},
                )
                return LeaseMutationResult.STALE_TOKEN_REJECTED
            # Administrative (unfenced) completion.
            if int(row["terminal"]) == 1:
                return LeaseMutationResult.ALREADY_APPLIED_IDEMPOTENTLY
            conn.execute(
                "UPDATE coverage_leases SET status=?, terminal=1, heartbeat_at=?, updated_at=? "
                "WHERE run_id=? AND coverage_id=?",
                (terminal_status, now, now, run_id, coverage_id),
            )
            self._append_lease_event(
                conn, coverage_id, run_id, "COMPLETE", worker_id=worker_id, at=now,
                detail={"terminal_status": terminal_status},
            )
            return LeaseMutationResult.APPLIED

    def release_coverage_lease(
        self, coverage_id: str, run_id: str, *, worker_id: Optional[str] = None,
        version: Optional[int] = None, now: str, requeue: bool = True,
    ) -> "LeaseMutationResult":
        """Release a held (non-terminal) lease. FENCED when ``worker_id``+
        ``version`` are supplied: only the current owner+token may release; a
        stale owner is refused and audited. A terminal child is never reopened.
        With ``requeue`` the child returns to ``AVAILABLE`` for another worker."""
        with self.immediate_transaction() as conn:
            row = conn.execute(
                "SELECT status, terminal, worker_id, version FROM coverage_leases "
                "WHERE run_id=? AND coverage_id=?",
                (run_id, coverage_id),
            ).fetchone()
            if row is None:
                return LeaseMutationResult.NOT_FOUND
            if int(row["terminal"]) == 1:
                return LeaseMutationResult.ALREADY_APPLIED_IDEMPOTENTLY  # terminal is never reopened
            new_status = "AVAILABLE" if requeue else "HELD_RELEASED"
            if worker_id is not None and version is not None:
                same_owner = row["worker_id"] == worker_id and int(row["version"]) == int(version)
                if same_owner and row["status"] == "LEASED":
                    conn.execute(
                        "UPDATE coverage_leases SET status=?, worker_id=NULL, lease_expires_at=NULL, updated_at=? "
                        "WHERE run_id=? AND coverage_id=? AND worker_id=? AND version=? AND status='LEASED' AND terminal=0",
                        (new_status, now, run_id, coverage_id, worker_id, version),
                    )
                    self._append_lease_event(
                        conn, coverage_id, run_id, "RELEASE", worker_id=worker_id, at=now, detail={"requeue": requeue},
                    )
                    return LeaseMutationResult.APPLIED
                self._append_lease_event(
                    conn, coverage_id, run_id, "RELEASE_REJECTED", worker_id=worker_id, at=now,
                    detail={"reason": "stale_token", "current_worker": row["worker_id"],
                            "current_version": int(row["version"])},
                )
                return LeaseMutationResult.STALE_TOKEN_REJECTED
            conn.execute(
                "UPDATE coverage_leases SET status=?, worker_id=NULL, lease_expires_at=NULL, updated_at=? "
                "WHERE run_id=? AND coverage_id=?",
                (new_status, now, run_id, coverage_id),
            )
            self._append_lease_event(
                conn, coverage_id, run_id, "RELEASE", worker_id=worker_id, at=now, detail={"requeue": requeue},
            )
            return LeaseMutationResult.APPLIED

    # ------------------------------------------------------------------
    # Phase 1C-A FINAL — FENCED business commits (build spec 3 + 5)
    # ------------------------------------------------------------------
    def _fence_current(
        self, conn, run_id: str, coverage_id: str, worker_id: Optional[str], version: Optional[int]
    ) -> bool:
        """True when ``(run_id, coverage_id, worker_id, version)`` still owns a
        non-terminal LEASED row. ``worker_id``/``version`` None means the caller
        holds no lease (the single-threaded sequential path) and is treated as
        current — there is no other worker to race. A different owner or a higher
        fencing version (a reclaim) makes this False so a stale worker's business
        commit is refused."""
        if worker_id is None or version is None:
            return True
        row = conn.execute(
            "SELECT status, terminal, worker_id, version FROM coverage_leases "
            "WHERE run_id=? AND coverage_id=?",
            (run_id, coverage_id),
        ).fetchone()
        if row is None:
            return False
        return (
            row["worker_id"] == worker_id
            and int(row["version"]) == int(version)
            and row["status"] == "LEASED"
            and int(row["terminal"]) == 0
        )

    def commit_child_page(
        self,
        run_id: str,
        coverage_id: str,
        *,
        worker_id: Optional[str] = None,
        version: Optional[int] = None,
        now: Optional[str] = None,
        page_index: int,
        page_status: str,
        cursor: Optional[str] = None,
        has_more: bool = False,
        total_reported: Optional[int] = None,
        request_fingerprint: Optional[str] = None,
        attempt_id: Optional[str] = None,
        next_page_index: Optional[int] = None,
        next_page_cursor: Optional[str] = None,
        attempts: tuple = (),
        health: Optional[dict] = None,
        observations: tuple = (),
    ) -> "ChildCommitResult":
        """Atomically persist ONE page's full result batch UNDER THE LEASE FENCE
        (build spec 3 + 5). In ONE ``immediate_transaction``: verify the fence,
        write every attempt row, write the health row, stage every observation,
        upsert this page DONE/FAILED (with ``has_more``/total/fingerprint), and —
        when a continuation is required — create the next PENDING page with the
        exact cursor. All-or-none.

        A stale (reclaimed) worker writes NOTHING business and gets
        ``STALE_TOKEN_REJECTED`` with an append-only ``BUSINESS_COMMIT_REJECTED``
        audit event. ``worker_id``/``version`` None = unfenced sequential path."""
        at = now or _utcnow()
        with self.immediate_transaction() as conn:
            if not self._fence_current(conn, run_id, coverage_id, worker_id, version):
                self._append_lease_event(
                    conn, coverage_id, run_id, "BUSINESS_COMMIT_REJECTED", worker_id=worker_id,
                    attempt=int(page_index), at=at,
                    detail={"reason": "stale_token", "kind": "page", "page_index": int(page_index)},
                )
                return ChildCommitResult(LeaseMutationResult.STALE_TOKEN_REJECTED, 0)
            for a in attempts:
                self._insert_coverage_attempt(conn, **a)
            if health is not None:
                self._insert_source_health(conn, **health)
            staged = 0
            for o in observations:
                cur = self._insert_raw_observation(conn, **o)
                if cur.rowcount:
                    staged += 1
            self._upsert_coverage_page(
                conn, run_id, coverage_id, page_index, status=page_status, cursor=cursor,
                has_more=has_more, results_count=staged, total_reported=total_reported,
                request_fingerprint=request_fingerprint, attempt_id=attempt_id, now=at,
            )
            if next_page_index is not None:
                self._upsert_coverage_page(
                    conn, run_id, coverage_id, next_page_index, status="PENDING",
                    cursor=next_page_cursor, has_more=False, now=at,
                )
            return ChildCommitResult(LeaseMutationResult.APPLIED, staged)

    def complete_child(
        self,
        run_id: str,
        coverage_id: str,
        source_instance: str,
        *,
        worker_id: Optional[str] = None,
        version: Optional[int] = None,
        now: Optional[str] = None,
        terminal_status: str,
        lease_terminal_status: Optional[str] = None,
        coverage_kwargs: Optional[dict] = None,
    ) -> "LeaseMutationResult":
        """Atomically record a child's terminal coverage row AND mark its lease
        terminal in ONE transaction under the fence (build spec 3). A stale
        worker writes neither — it gets ``STALE_TOKEN_REJECTED`` (audited) so a
        reclaimed worker can never overwrite the authoritative result. When no
        ``worker_id``/``version`` is supplied only the coverage row is written
        (the sequential path owns its lease separately)."""
        at = now or _utcnow()
        ck = dict(coverage_kwargs or {})
        lease_status = lease_terminal_status or terminal_status
        with self.immediate_transaction() as conn:
            if worker_id is not None and version is not None:
                row = conn.execute(
                    "SELECT status, terminal, worker_id, version FROM coverage_leases "
                    "WHERE run_id=? AND coverage_id=?",
                    (run_id, coverage_id),
                ).fetchone()
                if row is None:
                    return LeaseMutationResult.NOT_FOUND
                same_owner = row["worker_id"] == worker_id and int(row["version"]) == int(version)
                if int(row["terminal"]) == 1 and same_owner:
                    return LeaseMutationResult.ALREADY_APPLIED_IDEMPOTENTLY
                if not (same_owner and row["status"] == "LEASED" and int(row["terminal"]) == 0):
                    self._append_lease_event(
                        conn, coverage_id, run_id, "BUSINESS_COMMIT_REJECTED", worker_id=worker_id,
                        attempt=-1, at=at,
                        detail={"reason": "stale_token", "kind": "complete_child",
                                "current_worker": row["worker_id"], "current_version": int(row["version"])},
                    )
                    return LeaseMutationResult.STALE_TOKEN_REJECTED
                self._upsert_coverage(conn, coverage_id, run_id, source_instance, now=at, **ck)
                conn.execute(
                    "UPDATE coverage_leases SET status=?, terminal=1, heartbeat_at=?, updated_at=? "
                    "WHERE run_id=? AND coverage_id=? AND worker_id=? AND version=? AND status='LEASED' AND terminal=0",
                    (lease_status, at, at, run_id, coverage_id, worker_id, version),
                )
                self._append_lease_event(
                    conn, coverage_id, run_id, "COMPLETE", worker_id=worker_id, at=at,
                    detail={"terminal_status": lease_status, "fenced_child": True},
                )
                return LeaseMutationResult.APPLIED
            # Unfenced (sequential) path: write only the coverage row.
            self._upsert_coverage(conn, coverage_id, run_id, source_instance, now=at, **ck)
            return LeaseMutationResult.APPLIED

    def human_reopen_child(
        self, run_id: str, coverage_id: str, *, reason: str, reference: Optional[str] = None,
        op_key: str, now: Optional[str] = None,
    ) -> "LeaseMutationResult":
        """Explicit, authorized human reopen of ONE human-blocked child (build
        spec 4). In ONE transaction, and ONLY when the child's coverage status is
        BLOCKED_HUMAN: reset the coverage row to NOT_ATTEMPTED, reopen the lease
        (AVAILABLE, terminal=0, version++ — fencing any old token) WHEN a lease
        row exists (the parallel path), append a HUMAN_REOPEN audit event, and
        record the idempotency ``op_key``. All-or-none, so coverage and lease can
        never diverge if a fault occurs mid-reopen. Idempotent by ``op_key``:
        a duplicate reopen is a harmless no-op. A normally-completed / non-blocked
        child is NEVER reopened."""
        at = now or _utcnow()
        with self.immediate_transaction() as conn:
            if conn.execute("SELECT 1 FROM applied_operations WHERE op_key=?", (op_key,)).fetchone():
                return LeaseMutationResult.ALREADY_APPLIED_IDEMPOTENTLY
            cov = conn.execute(
                "SELECT status FROM coverage_records WHERE run_id=? AND coverage_id=?",
                (run_id, coverage_id),
            ).fetchone()
            if cov is None:
                return LeaseMutationResult.NOT_FOUND
            if cov["status"] != "BLOCKED_HUMAN":
                # Only a human-blocked child may be reopened; a normal terminal
                # child is never reopened.
                return LeaseMutationResult.STALE_TOKEN_REJECTED
            conn.execute(
                "UPDATE coverage_records SET status='NOT_ATTEMPTED', attempted=0, completed=0, "
                "next_action='SEARCH', updated_at=? WHERE run_id=? AND coverage_id=?",
                (at, run_id, coverage_id),
            )
            lease = conn.execute(
                "SELECT version FROM coverage_leases WHERE run_id=? AND coverage_id=?",
                (run_id, coverage_id),
            ).fetchone()
            new_version = None
            if lease is not None:
                new_version = int(lease["version"]) + 1
                conn.execute(
                    "UPDATE coverage_leases SET status='AVAILABLE', terminal=0, worker_id=NULL, "
                    "lease_expires_at=NULL, heartbeat_at=NULL, version=?, updated_at=? "
                    "WHERE run_id=? AND coverage_id=?",
                    (new_version, at, run_id, coverage_id),
                )
            self._append_lease_event(
                conn, coverage_id, run_id, "HUMAN_REOPEN", worker_id=None, at=at,
                detail={"reason": reason, "reference": reference, "new_version": new_version},
            )
            conn.execute(
                "INSERT OR IGNORE INTO applied_operations (op_key, applied_at, result_json) VALUES (?, ?, ?)",
                (op_key, at, json.dumps({"coverage_id": coverage_id, "new_version": new_version}, sort_keys=True)),
            )
            return LeaseMutationResult.APPLIED

    # ------------------------------------------------------------------
    # Phase 1C-A FINAL — durable verification decisions (build spec 10)
    # ------------------------------------------------------------------
    def record_verification_decision(
        self,
        decision_id: str,
        run_id: str,
        *,
        canonical_id: Optional[str] = None,
        evidence_revision_ids: Optional[list] = None,
        verification_level: str,
        lifecycle_result: Optional[str] = None,
        freshness_result: Optional[str] = None,
        closure_evidence: Optional[str] = None,
        classifier_version: str = "",
        policy_fingerprint: str = "",
        reasoning_ref: Optional[str] = None,
        decided_at: Optional[str] = None,
    ) -> bool:
        """Append an immutable per-job verification decision (idempotent on
        ``decision_id``). The report is derived from THIS persisted decision, not
        re-derived from an adapter label (build spec 10)."""
        with self._auto() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO verification_decisions (decision_id, run_id, canonical_id, "
                "evidence_revision_ids, verification_level, lifecycle_result, freshness_result, "
                "closure_evidence, classifier_version, policy_fingerprint, reasoning_ref, decided_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    decision_id, run_id, canonical_id,
                    json.dumps(list(evidence_revision_ids or []), sort_keys=True),
                    verification_level, lifecycle_result, freshness_result, closure_evidence,
                    classifier_version, policy_fingerprint, reasoning_ref, decided_at or _utcnow(),
                ),
            )
        return bool(cur.rowcount)

    def list_verification_decisions(self, run_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM verification_decisions WHERE run_id = ? ORDER BY decision_id", (run_id,)
        ).fetchall()

    def get_verification_decision(self, decision_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM verification_decisions WHERE decision_id = ?", (decision_id,)
        ).fetchone()

    def current_run_canonical_ids(self, run_id: str) -> list[str]:
        """Distinct canonical ids TOUCHED by this run's observations (build spec
        9). This is the only correct basis for a current-run summary — never the
        global canonical-job table which also holds prior runs' jobs."""
        rows = self._conn.execute(
            "SELECT DISTINCT canonical_id FROM raw_discovery_observations "
            "WHERE run_id = ? AND canonical_id IS NOT NULL ORDER BY canonical_id",
            (run_id,),
        ).fetchall()
        return [r["canonical_id"] for r in rows]

    def count_current_run_canonical_jobs(self, run_id: str) -> int:
        """Count of DISTINCT canonical jobs touched by this run (build spec 9)."""
        return int(
            self._conn.execute(
                "SELECT COUNT(DISTINCT canonical_id) AS n FROM raw_discovery_observations "
                "WHERE run_id = ? AND canonical_id IS NOT NULL",
                (run_id,),
            ).fetchone()["n"]
        )

    def reopen_human_coverage_lease(
        self, coverage_id: str, run_id: str, *, now: str, reason: str = "", reference: Optional[str] = None,
    ) -> "LeaseMutationResult":
        """Explicit, authorized reopen of a BLOCKED_HUMAN child (build spec 9).

        Resets the lease to AVAILABLE, clears owner/expiry/heartbeat, INCREMENTS
        the version (fencing any old token), and appends a HUMAN_REOPEN audit
        event with the resolution reason/reference. ONLY a child whose lease is
        human-blocked may be reopened — a normally-completed/terminal child is
        never reopened, and ordinary retry/resume must not call this."""
        with self.immediate_transaction() as conn:
            row = conn.execute(
                "SELECT status, terminal, version FROM coverage_leases WHERE run_id=? AND coverage_id=?",
                (run_id, coverage_id),
            ).fetchone()
            if row is None:
                return LeaseMutationResult.NOT_FOUND
            if row["status"] != "BLOCKED_HUMAN":
                # Not human-blocked: refuse to reopen (a normal terminal child).
                return LeaseMutationResult.STALE_TOKEN_REJECTED
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE coverage_leases SET status='AVAILABLE', terminal=0, worker_id=NULL, "
                "lease_expires_at=NULL, heartbeat_at=NULL, version=?, updated_at=? "
                "WHERE run_id=? AND coverage_id=?",
                (version, now, run_id, coverage_id),
            )
            self._append_lease_event(
                conn, coverage_id, run_id, "HUMAN_REOPEN", worker_id=None, at=now,
                detail={"reason": reason, "reference": reference, "new_version": version},
            )
            return LeaseMutationResult.APPLIED

    def get_coverage_lease(self, coverage_id: str, run_id: Optional[str] = None) -> Optional[sqlite3.Row]:
        """Fetch one lease row. ``run_id`` disambiguates the same logical
        ``coverage_id`` across runs (composite PK ``(run_id, coverage_id)``)."""
        if run_id is not None:
            return self._conn.execute(
                "SELECT * FROM coverage_leases WHERE run_id = ? AND coverage_id = ?",
                (run_id, coverage_id),
            ).fetchone()
        return self._conn.execute(
            "SELECT * FROM coverage_leases WHERE coverage_id = ? ORDER BY run_id LIMIT 1", (coverage_id,)
        ).fetchone()

    def list_coverage_leases(self, run_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM coverage_leases WHERE run_id = ? ORDER BY coverage_id", (run_id,)
        ).fetchall()

    def count_active_coverage_leases(self, run_id: str, *, now: str) -> int:
        return int(self._conn.execute(
            "SELECT COUNT(*) AS n FROM coverage_leases WHERE run_id=? AND status='LEASED' AND terminal=0 "
            "AND (lease_expires_at IS NULL OR lease_expires_at > ?)",
            (run_id, now),
        ).fetchone()["n"])

    def list_coverage_lease_events(
        self, *, coverage_id: Optional[str] = None, run_id: Optional[str] = None
    ) -> list[sqlite3.Row]:
        if coverage_id is not None:
            return self._conn.execute(
                "SELECT * FROM coverage_lease_events WHERE coverage_id = ? ORDER BY at, rowid",
                (coverage_id,),
            ).fetchall()
        if run_id is not None:
            return self._conn.execute(
                "SELECT * FROM coverage_lease_events WHERE run_id = ? ORDER BY at, rowid",
                (run_id,),
            ).fetchall()
        return self._conn.execute(
            "SELECT * FROM coverage_lease_events ORDER BY at, rowid"
        ).fetchall()

    # ------------------------------------------------------------------
    # Phase 1C-A corrective — durable run-scoped pagination cursors (spec 10)
    # ------------------------------------------------------------------
    def upsert_coverage_page(
        self,
        run_id: str,
        coverage_id: str,
        page_index: int,
        *,
        status: str,
        cursor: Optional[str] = None,
        has_more: bool = False,
        results_count: int = 0,
        total_reported: Optional[int] = None,
        request_fingerprint: Optional[str] = None,
        attempt_id: Optional[str] = None,
    ) -> None:
        """Record/advance the durable state of ONE page of a coverage child
        (idempotent on ``(run_id, coverage_id, page_index)``). A child cannot be
        declared terminal-COMPLETE while any of its page rows remains non-DONE or
        a DONE page still reports ``has_more`` with an unfulfilled continuation
        (build spec 10)."""
        now = _utcnow()
        with self._auto() as conn:
            self._upsert_coverage_page(
                conn, run_id, coverage_id, page_index, status=status, cursor=cursor,
                has_more=has_more, results_count=results_count, total_reported=total_reported,
                request_fingerprint=request_fingerprint, attempt_id=attempt_id, now=now,
            )

    def _upsert_coverage_page(
        self, conn, run_id, coverage_id, page_index, *, status, cursor=None, has_more=False,
        results_count=0, total_reported=None, request_fingerprint=None, attempt_id=None, now=None,
    ):
        """Conn-scoped page upsert shared by the standalone call and the atomic
        fenced page commit."""
        now = now or _utcnow()
        conn.execute(
            "INSERT INTO coverage_page_state (run_id, coverage_id, page_index, cursor, status, "
            "has_more, results_count, total_reported, request_fingerprint, attempt_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id, coverage_id, page_index) DO UPDATE SET cursor=excluded.cursor, "
            "status=excluded.status, has_more=excluded.has_more, results_count=excluded.results_count, "
            "total_reported=excluded.total_reported, request_fingerprint=excluded.request_fingerprint, "
            "attempt_id=excluded.attempt_id, updated_at=excluded.updated_at",
            (run_id, coverage_id, int(page_index), cursor, status, 1 if has_more else 0,
             int(results_count), total_reported, request_fingerprint, attempt_id, now, now),
        )

    def list_coverage_pages(self, run_id: str, coverage_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM coverage_page_state WHERE run_id=? AND coverage_id=? ORDER BY page_index",
            (run_id, coverage_id),
        ).fetchall()

    def get_coverage_page(self, run_id: str, coverage_id: str, page_index: int) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM coverage_page_state WHERE run_id=? AND coverage_id=? AND page_index=?",
            (run_id, coverage_id, int(page_index)),
        ).fetchone()

    def coverage_pages_outstanding(self, run_id: str, coverage_id: str) -> bool:
        """True when a required next page/cursor remains for this child: a page
        row is still PENDING (a durably-created continuation not yet fetched), or
        the last DONE page still reports has_more (its continuation was never
        created). A FAILED page is a terminal outcome, NOT an outstanding
        continuation. Used to forbid a false terminal COMPLETE."""
        rows = self.list_coverage_pages(run_id, coverage_id)
        if not rows:
            return False
        for r in rows:
            if r["status"] == "PENDING":
                return True
        last = rows[-1]
        return last["status"] == "DONE" and bool(last["has_more"])

    # ------------------------------------------------------------------
    # Phase 1B — coverage plan lifecycle (build spec 7.8)
    # ------------------------------------------------------------------
    def upsert_coverage_plan(
        self,
        run_id: str,
        state: str,
        *,
        no_work_due: bool = False,
        fingerprint: Optional[str] = None,
        policy_fingerprint: Optional[str] = None,
        failure_reason: Optional[str] = None,
        sealed_at: Optional[str] = None,
    ) -> None:
        now = _utcnow()
        with self._auto() as conn:
            conn.execute(
                "INSERT INTO coverage_plans (run_id, state, no_work_due, fingerprint, "
                "policy_fingerprint, failure_reason, sealed_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET state=excluded.state, "
                "no_work_due=excluded.no_work_due, fingerprint=excluded.fingerprint, "
                "policy_fingerprint=excluded.policy_fingerprint, "
                "failure_reason=excluded.failure_reason, sealed_at=excluded.sealed_at, "
                "updated_at=excluded.updated_at",
                (
                    run_id, state, 1 if no_work_due else 0, fingerprint, policy_fingerprint,
                    failure_reason, sealed_at, now, now,
                ),
            )

    def get_coverage_plan(self, run_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM coverage_plans WHERE run_id = ?", (run_id,)
        ).fetchone()

    # ------------------------------------------------------------------
    # Phase 1A.5 — persistent company identity registry
    # ------------------------------------------------------------------
    def upsert_company(
        self,
        company_id: str,
        canonical_name: str,
        identity_key: str,
        *,
        display_name: str = "",
        official_domain: Optional[str] = None,
        careers_url: Optional[str] = None,
        country: Optional[str] = None,
        status: str = "ACTIVE",
        discovered_at: Optional[str] = None,
        last_verified_at: Optional[str] = None,
        provenance: Optional[dict] = None,
    ) -> str:
        """Insert or update a company identity record. Returns 'created' or
        'updated'. company_id is stable; display-name/domain updates never
        create a new record."""
        now = _utcnow()
        existing = self._conn.execute(
            "SELECT company_id FROM company_registry WHERE company_id = ?", (company_id,)
        ).fetchone()
        if existing is None:
            with self._auto() as conn:
                conn.execute(
                    "INSERT INTO company_registry (company_id, canonical_name, display_name, identity_key, "
                    "official_domain, careers_url, country, status, discovered_at, last_verified_at, "
                    "provenance_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        company_id, canonical_name, display_name, identity_key, official_domain,
                        careers_url, country, status, discovered_at or now, last_verified_at,
                        json.dumps(provenance or {}, sort_keys=True), now, now,
                    ),
                )
            return "created"
        with self._auto() as conn:
            # COALESCE so a later observation never nulls out a known value.
            conn.execute(
                "UPDATE company_registry SET canonical_name=?, display_name=?, identity_key=?, "
                "official_domain=COALESCE(?, official_domain), careers_url=COALESCE(?, careers_url), "
                "country=COALESCE(?, country), status=?, last_verified_at=COALESCE(?, last_verified_at), "
                "updated_at=? WHERE company_id=?",
                (
                    canonical_name, display_name, identity_key, official_domain, careers_url,
                    country, status, last_verified_at, now, company_id,
                ),
            )
        return "updated"

    def get_company(self, company_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM company_registry WHERE company_id = ?", (company_id,)
        ).fetchone()

    def find_company_by_domain(self, official_domain: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM company_registry WHERE official_domain = ? ORDER BY company_id LIMIT 1",
            (official_domain,),
        ).fetchone()

    def find_companies_by_identity_key(self, identity_key: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM company_registry WHERE identity_key = ? ORDER BY company_id",
            (identity_key,),
        ).fetchall()

    def find_company_id_by_alias_key(self, alias_key: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT company_id FROM company_aliases WHERE alias_key = ? ORDER BY company_id LIMIT 1",
            (alias_key,),
        ).fetchone()
        return row["company_id"] if row is not None else None

    def list_companies(self, limit: int = 1000) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM company_registry ORDER BY canonical_name, company_id LIMIT ?", (int(limit),)
        ).fetchall()

    def count_companies(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) AS n FROM company_registry").fetchone()["n"])

    def set_company_domain(self, company_id: str, official_domain: str) -> None:
        with self._auto() as conn:
            conn.execute(
                "UPDATE company_registry SET official_domain=?, updated_at=? WHERE company_id=?",
                (official_domain, _utcnow(), company_id),
            )

    def update_company_verification(
        self, company_id: str, *, last_verified_at: Optional[str] = None, status: Optional[str] = None
    ) -> None:
        now = _utcnow()
        with self._auto() as conn:
            if status is not None:
                conn.execute(
                    "UPDATE company_registry SET last_verified_at=?, status=?, updated_at=? WHERE company_id=?",
                    (last_verified_at or now, status, now, company_id),
                )
            else:
                conn.execute(
                    "UPDATE company_registry SET last_verified_at=?, updated_at=? WHERE company_id=?",
                    (last_verified_at or now, now, company_id),
                )

    def add_company_alias(
        self, alias_id: str, company_id: str, alias: str, alias_key: str, *, source: str = "explicit"
    ) -> bool:
        """Add an alias (idempotent on (company_id, alias_key)). Returns True
        if a new alias row was created."""
        with self._auto() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO company_aliases (alias_id, company_id, alias, alias_key, source, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (alias_id, company_id, alias, alias_key, source, _utcnow()),
            )
        return bool(cur.rowcount)

    def list_company_aliases(self, company_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM company_aliases WHERE company_id = ? ORDER BY alias_key", (company_id,)
        ).fetchall()

    # ------------------------------------------------------------------
    # Phase 1A.5 — company↔source relationships
    # ------------------------------------------------------------------
    def upsert_source_relationship(
        self,
        relationship_id: str,
        company_id: str,
        instance_id: str,
        source_type: str,
        *,
        base_url: Optional[str] = None,
        tenant: Optional[str] = None,
        state: str = "DISCOVERED",
        confidence: str = "UNKNOWN",
        is_current: bool = True,
        discovered_at: Optional[str] = None,
        provenance: Optional[dict] = None,
    ) -> str:
        """Insert or update a company↔source relationship (idempotent on
        relationship_id). Returns 'created' or 'updated'."""
        now = _utcnow()
        existing = self._conn.execute(
            "SELECT relationship_id FROM company_source_relationships WHERE relationship_id = ?",
            (relationship_id,),
        ).fetchone()
        if existing is None:
            with self._auto() as conn:
                conn.execute(
                    "INSERT INTO company_source_relationships (relationship_id, company_id, instance_id, "
                    "source_type, base_url, tenant, state, confidence, is_current, discovered_at, updated_at, "
                    "provenance_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        relationship_id, company_id, instance_id, source_type, base_url, tenant, state,
                        confidence, 1 if is_current else 0, discovered_at or now, now,
                        json.dumps(provenance or {}, sort_keys=True),
                    ),
                )
            return "created"
        with self._auto() as conn:
            conn.execute(
                "UPDATE company_source_relationships SET source_type=?, base_url=COALESCE(?, base_url), "
                "tenant=COALESCE(?, tenant), state=?, confidence=?, is_current=?, updated_at=? "
                "WHERE relationship_id=?",
                (source_type, base_url, tenant, state, confidence, 1 if is_current else 0, now, relationship_id),
            )
        return "updated"

    def get_source_relationship(self, relationship_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM company_source_relationships WHERE relationship_id = ?", (relationship_id,)
        ).fetchone()

    def list_relationships_for_company(self, company_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM company_source_relationships WHERE company_id = ? ORDER BY relationship_id",
            (company_id,),
        ).fetchall()

    def list_relationships_for_instance(self, instance_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM company_source_relationships WHERE instance_id = ? ORDER BY relationship_id",
            (instance_id,),
        ).fetchall()

    def set_relationship_state(
        self, relationship_id: str, state: str, *, is_current: Optional[bool] = None
    ) -> None:
        now = _utcnow()
        with self._auto() as conn:
            if is_current is None:
                conn.execute(
                    "UPDATE company_source_relationships SET state=?, updated_at=? WHERE relationship_id=?",
                    (state, now, relationship_id),
                )
            else:
                conn.execute(
                    "UPDATE company_source_relationships SET state=?, is_current=?, updated_at=? WHERE relationship_id=?",
                    (state, 1 if is_current else 0, now, relationship_id),
                )

    def count_source_relationships(self) -> int:
        return int(
            self._conn.execute("SELECT COUNT(*) AS n FROM company_source_relationships").fetchone()["n"]
        )

    # ------------------------------------------------------------------
    # Phase 1A.5 — source discovery observations (append-only provenance)
    # ------------------------------------------------------------------
    def add_source_discovery_observation(
        self,
        observation_id: str,
        company_id: str,
        method: str,
        *,
        instance_id: Optional[str] = None,
        input_url: Optional[str] = None,
        resolved_url: Optional[str] = None,
        detected_ats: Optional[str] = None,
        tenant: Optional[str] = None,
        confidence: str = "UNKNOWN",
        evidence_ref: Optional[str] = None,
        verification_state: str = "UNVERIFIED",
        observed_at: Optional[str] = None,
        detail: Optional[dict] = None,
    ) -> bool:
        """Append a discovery observation (idempotent on observation_id;
        historical evidence is never overwritten). Returns True if new."""
        with self._auto() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO source_discovery_observations (observation_id, company_id, instance_id, "
                "method, input_url, resolved_url, detected_ats, tenant, confidence, evidence_ref, "
                "verification_state, observed_at, detail_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    observation_id, company_id, instance_id, method, input_url, resolved_url, detected_ats,
                    tenant, confidence, evidence_ref, verification_state, observed_at or _utcnow(),
                    json.dumps(detail or {}, sort_keys=True),
                ),
            )
        return bool(cur.rowcount)

    def list_source_discovery_observations(self, company_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM source_discovery_observations WHERE company_id = ? ORDER BY observed_at, observation_id",
            (company_id,),
        ).fetchall()


@contextlib.contextmanager
def open_store(db_path: Path) -> Iterator[StateStore]:
    store = StateStore(db_path)
    try:
        yield store
    finally:
        store.close()
