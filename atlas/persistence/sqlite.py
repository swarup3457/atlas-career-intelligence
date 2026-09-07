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
    ) -> bool:
        """Append an observation. Idempotent: a duplicate ``observation_id``
        is ignored (returns False). Increments the canonical observation
        count only for genuinely new observations."""
        with self._auto() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO job_observations (observation_id, canonical_id, record_id, "
                "source_file, sheet_name, discovery_source, observed_status, observed_at, "
                "content_hash, provenance_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    observation_id, canonical_id, record_id, source_file, sheet_name,
                    discovery_source, observed_status, observed_at, content_hash,
                    json.dumps(provenance or {}, sort_keys=True), _utcnow(),
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


@contextlib.contextmanager
def open_store(db_path: Path) -> Iterator[StateStore]:
    store = StateStore(db_path)
    try:
        yield store
    finally:
        store.close()
