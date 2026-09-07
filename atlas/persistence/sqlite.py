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
import datetime
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterator, Optional


class MigrationError(RuntimeError):
    """Raised when a schema migration fails; the failed migration's
    statements are rolled back and NOT recorded as applied."""


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
            conn.execute(
                "INSERT INTO coverage_records (coverage_id, run_id, company, source_instance, "
                "source_type, lane, query_key, attempted, completed, status, jobs_found, new_jobs, "
                "changed_jobs, closed_jobs, started_at, completed_at, next_action, detail_json, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(coverage_id) DO UPDATE SET company=excluded.company, "
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

    def get_coverage(self, coverage_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM coverage_records WHERE coverage_id = ?", (coverage_id,)
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
    ) -> bool:
        """Append a source health/yield observation (idempotent on
        ``history_id``). Enough deterministic history to detect a future
        false zero (recent yields 42,38,47 then 0)."""
        esp = None if expected_structure_present is None else (1 if expected_structure_present else 0)
        with self._auto() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO source_health_history (history_id, source_instance, source_type, "
                "state, result_count, expected_structure_present, reason, observed_at, evidence_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    history_id, source_instance, source_type, state, result_count, esp, reason,
                    observed_at or _utcnow(), json.dumps(evidence or {}, sort_keys=True),
                ),
            )
        return bool(cur.rowcount)

    def list_source_health(self, source_instance: str, limit: int = 20) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM source_health_history WHERE source_instance = ? "
            "ORDER BY observed_at DESC, history_id DESC LIMIT ?",
            (source_instance, int(limit)),
        ).fetchall()

    def recent_yields(self, source_instance: str, limit: int = 5) -> list[int]:
        """Most-recent non-null result counts (newest first) for false-zero
        detection."""
        rows = self._conn.execute(
            "SELECT result_count FROM source_health_history WHERE source_instance = ? "
            "AND result_count IS NOT NULL ORDER BY observed_at DESC, history_id DESC LIMIT ?",
            (source_instance, int(limit)),
        ).fetchall()
        return [int(r["result_count"]) for r in rows]

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
