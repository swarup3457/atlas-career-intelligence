# Atlas Backup &amp; Restore (Phase 0.95)

Atlas keeps its authoritative run state in **local SQLite** — the durable
state DB (`state/atlas_state.sqlite`) and the LangGraph checkpoint DB
(`state/atlas_checkpoints.sqlite`). A disaster (disk loss, corruption, an
accidental delete) that takes those files with it takes the run's progress
with it, unless there is a **verified, consistent, restorable backup**.

Phase 0.95 adds that machinery in `atlas/backup/`. It is pure platform
plumbing — no job-search business logic, no scheduling, no LLM.

---

## What is (and is not) backed up

| Component | Status | Why |
| --- | --- | --- |
| `state/atlas_state.sqlite` | **BACKED UP** | authoritative durable run state (consistent online snapshot) |
| `state/atlas_checkpoints.sqlite` | **BACKED UP** | LangGraph resume checkpoints (consistent online snapshot) |
| `config/default.yaml`, `config/local.yaml` | **BACKED UP (sanitized)** | reproducibility; secret-named keys redacted to `***REDACTED***` |
| resolved `Settings` | **BACKED UP (sanitized)** | records the effective configuration; `Path`s stringified |
| `agents/` | **BACKED UP** | agent definitions are part of run reproducibility |
| `skills/` | **BACKED UP** | skill definitions are part of run reproducibility |
| `output/run_manifest_*.json` | **BACKED UP** | run/schema metadata |
| `.browser-profile-chrome`, `.browser-profile` | **MACHINE LOCAL — NOT BACKED UP** | authenticated Chrome profile; cookies/session; **requires re-authentication** after restore |
| secrets / credentials / tokens / cookies | **NEVER BACKED UP** | never captured; sensitively-named config keys are redacted |
| `.venv` | **NOT BACKED UP** | reproducible from `pyproject.toml` + `requirements-lock.txt` |
| caches (`__pycache__`, `.pytest_cache`, `*.pyc`) | **NOT BACKED UP** | regenerable noise |
| `fixtures/real/` | **NOT BACKED UP** | immutable, source-controlled input fixture; never duplicated or overwritten |
| `fixtures/generated/` | **NOT BACKED UP** | disposable Phase 0.9 output; recreate on demand |
| `logs/` | **NOT BACKED UP** | may contain sensitive runtime detail; a *sanitized tail* is available via `atlas support-bundle` |
| `output/` reports (xlsx/json) | **NOT BACKED UP** | regenerable from restored state |

The manifest records both lists explicitly (`included_components` /
`excluded_components`), so every backup is self-describing.

---

## Consistent SQLite backups (WAL-safe)

Atlas SQLite DBs run in **WAL** mode. A naive file copy can catch a *torn*
state — recently committed pages may live only in the sibling `-wal` file,
so the copied main file alone is stale or inconsistent.

`atlas/backup/sqlite_backup.py` therefore uses SQLite's **online backup
API** (`sqlite3.Connection.backup()`), which walks a read transaction over
the live database and writes a **transactionally consistent snapshot** into
a brand-new, already-checkpointed database file. It is safe with WAL and
with a concurrent writer. The source is opened **read-only** (`mode=ro`)
so a backup can never mutate the live database.

Proven by `tests/test_phase095_backup.py::
test_consistent_sqlite_backup_captures_committed_rows_with_wal_open`, which
keeps a `StateStore` open (WAL active) while backing up and confirms the
snapshot returns exactly the committed rows and passes `PRAGMA
integrity_check`.

---

## The write protocol (atomic, self-verifying)

`create_backup(settings, output_dir, clock=None)` mirrors the Phase 0.9
report-writer discipline:

1. **stage** — everything is assembled under a private
   `.staging-<backup_id>` directory.
2. **checksum + manifest** — every file gets a streaming SHA-256 and size;
   `manifest.json` is written into the staging dir.
3. **verify** — `verify_backup()` re-reads the staging dir from disk and
   re-checks every checksum (and SQLite integrity). A mismatch aborts
   *before* anything is published.
4. **atomic publish** — the staging dir is `os.replace`d onto its final
   name `output_dir/<backup_id>` (same volume → atomic). Windows
   AV/indexer sharing violations are absorbed with a short bounded retry.

A process killed at any point before step 4 leaves only a throwaway
`.staging-*` dir; `verify_backup()` at the final path finds nothing valid,
and any previously-published backup is untouched.

### Backup identity

`backup_id = <compact-utc-stamp>-<ULID>` e.g.
`20260906T180000Z-01J8Z...`. The leading UTC stamp makes a directory
listing human-chronological; the trailing ULID (80 bits of randomness) makes
it collision-resistant and lexicographically sortable by creation time. See
`atlas/utils/ids.py`.

---

## CLI usage

```powershell
# Create a verified backup (default: <output_dir>/backups/<backup_id>)
atlas backup
atlas backup --output D:\atlas-backups
atlas backup --keep-latest 7           # create, then prune to newest 7

# Verify an existing backup directory
atlas backup verify D:\atlas-backups\20260906T180000Z-01J8Z...

# Restore a verified backup into a DISPOSABLE target directory
atlas restore D:\atlas-backups\20260906T180000Z-01J8Z... --target D:\atlas-restore-test

# Write a sanitized diagnostic support bundle (zip)
atlas support-bundle
```

`atlas backup` prints the backup id, created-at, path, state schema
version, total size, the included component list (with truncated
checksums), and the excluded component list — matching the plain-text
style of `atlas status` / `atlas version`.

---

## Verify

`verify_backup(backup_path) -> VerifyResult` returns a **specific** list of
problems, never a bare boolean:

| Problem kind | Meaning |
| --- | --- |
| `MANIFEST_MISSING` | no `manifest.json` (or backup dir absent) |
| `MANIFEST_MALFORMED` | `manifest.json` is not parseable / wrong shape |
| `MISSING_FILE` | a manifest-listed file is not present |
| `HASH_MISMATCH` | a file's SHA-256 differs from the manifest |
| `SIZE_MISMATCH` | a file's size differs from the manifest |
| `CORRUPT_DB` | a listed `*.sqlite` fails `PRAGMA integrity_check` |

Exit code is `0` on pass, `1` on fail.

---

## Restore

`restore_backup(backup_path, target_dir) -> RestoreResult` is deliberately
conservative and refuses unless **all** hold:

1. **verifies** — `verify_backup()` must pass first (a corrupt backup is
   never restored over anything);
2. **schema-compatible** — the backup's `state_schema_version` must not be
   *newer* than this codebase's `StateStore.SCHEMA_VERSION`. Equal or older
   is allowed (forward migrations can run). Patch-level `atlas_version`
   differences are **not** treated as incompatible;
3. **safe target** — `target_dir` must be a caller-provided disposable
   directory. Restoring onto the real project root, the live `state/`
   directory, `fixtures/real`, `.venv`, or a browser profile raises
   `RestoreSafetyError`.

Files are copied into `target_dir` preserving relative paths (parents
auto-created). Restored artifacts are then re-validated in place — SQLite
integrity + schema query, checkpoint DB opens via `open_checkpointer`,
config export parses — and only then is a `_RESTORE_OK` marker written and
status `RESTORED` returned.

Statuses: `RESTORED`, `REFUSED_INVALID_BACKUP`,
`REFUSED_INCOMPATIBLE_SCHEMA`, `FAILED_VALIDATION`.

---

## Retention

`apply_retention(backups_dir, keep_latest_n) -> [deleted backup_ids]` keeps
the newest N valid backups (ordered by `created_at`, tie-broken by the
sortable `backup_id`) and deletes the rest. It only considers immediate
subdirectories that contain a parseable `manifest.json` (in-flight
`.staging-*` dirs and non-backup dirs are ignored), and it **re-checks that
every deletion target is a strict subpath of `backups_dir`** before
removing it — a candidate resolving outside raises `RetentionSafetyError`.

---

## Support bundle

`atlas support-bundle` writes a small sanitized zip for diagnostics
containing only: `info.json` (versions + config fingerprint + schema
version), `doctor.txt` (`run_doctor().render()`), `atlas_log_tail.log` (a
redacted tail — any line mentioning a secret keyword is dropped),
`error_summary.txt`, and `run_manifest.json` (most recent, if any). It
never includes cookies, credentials, tokens, browser storage, or resume
files. Written atomically (`.part` temp → `os.replace`).

---

## Related docs

* `docs/DISASTER_RECOVERY.md` — the disaster drill, corruption handling,
  browser-profile re-authentication policy, safe-reset guarantees.
* `docs/REPRODUCIBILITY.md` — fresh-clone bootstrap and the two-tier
  dependency strategy.
* `docs/REPORT_RELIABILITY.md` — the Phase 0.9 atomic write protocol reused
  here.
