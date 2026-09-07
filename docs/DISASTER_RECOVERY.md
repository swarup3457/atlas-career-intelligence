# Atlas Disaster Recovery (Phase 0.95)

This document describes what happens when Atlas's local state is lost or
damaged, how the backup machinery recovers from it, and the guarantees the
recovery tooling makes. It complements `docs/BACKUP_RESTORE.md` (the
mechanism reference) and `docs/FAILURE_RECOVERY.md` (in-run crash recovery).

---

## The disaster drill (automated)

The core promise of Phase 0.95 is proven end-to-end, fully offline, in
`tests/test_phase095_disaster.py::
test_full_disaster_recovery_resume_without_reexecution`. The narrative:

1. **Build disposable state.** A fresh `AtlasRuntime` runs the deterministic
   50-task demo workload with `stop_after_completed=15`, producing a
   **PARTIAL** run — 15 tasks done, 35 remaining — with real durable state
   and LangGraph checkpoints under a `tmp_path`.
2. **Back it up.** `create_backup()` captures a consistent snapshot of the
   state DB and checkpoint DB (plus config/agents/skills). `verify_backup()`
   confirms it is sound.
3. **Simulate destruction.** The entire `state/` directory (both SQLite
   DBs) is deleted — the machine has "lost" its state.
4. **Restore elsewhere.** `restore_backup()` restores into a *fresh*
   `tmp_path` target, re-validating the SQLite integrity, checkpoint
   openability, and config parse.
5. **Resume as a new process.** A brand-new `AtlasRuntime` instance
   (simulating a fresh Python process) is pointed at the **restored**
   state and checkpoint DBs and calls `.resume()`.
6. **It completes.** The run reaches **COMPLETE** with all 50 tasks done.
7. **No work is repeated.** A shared attempt counter proves that **every
   task completed before the crash keeps its exact pre-crash attempt
   count** — none of the 15 already-done tasks is re-executed on resume.
   The completed set only grows (15 → 50), never shrinks or duplicates.

This is the whole point of a consistent checkpoint backup: recovery resumes
*from where it was*, it does not restart.

A second, broader **clean-room** test
(`test_clean_room_end_to_end`) chains `run_doctor` (asserting no `FAIL`) →
partial run → backup → state loss → restore → resume → **COMPLETE** → a
report artifact exists → `verify_backup` still passes. Fully offline, no
`real_web`.

---

## Corruption &amp; incompleteness handling

| Scenario | Detection | Test |
| --- | --- | --- |
| A backed-up file's bytes are flipped | `HASH_MISMATCH` (file flagged by relative path) | `test_corrupted_backup_flagged_by_verify` |
| A backed-up SQLite DB is corrupted | flagged (hash first; `CORRUPT_DB` via `PRAGMA integrity_check` when bytes still hash-match) | `test_corrupted_state_db_detected` |
| A manifest-listed file is missing | `MISSING_FILE` (specific missing-file diagnostic) | `test_incomplete_backup_missing_file_flagged` |
| `manifest.json` is malformed | `MANIFEST_MALFORMED` | `test_malformed_manifest_flagged` |
| A process is killed mid-backup | no valid backup at the target; staging dir discarded; **prior backup untouched** | `test_interrupted_backup_leaves_no_valid_backup_and_prior_untouched` |
| A locked publish destination | fails cleanly (controlled `PermissionError`); no partial/valid backup; no leftover staging; prior backup intact | `test_backup_locked_publish_is_controlled_no_corruption` |

A restore always runs `verify_backup()` first, so **a corrupt or incomplete
backup can never be restored over anything** (`REFUSED_INVALID_BACKUP`).

---

## Browser profile: machine-local, requires re-authentication

The authenticated Chrome profiles (`.browser-profile-chrome`,
`.browser-profile`) are **deliberately never backed up**. They contain
cookies and live session state that are:

* **machine-local and non-portable** — restoring them onto another machine
  or after a credential rotation would not produce a working session; and
* **sensitive** — Atlas never captures cookies/credentials into a backup.

Therefore, after a disaster restore, Atlas's durable state and checkpoints
come back intact and resumable, **but any browser session must be
re-established manually**. Recovery tooling models this explicitly with an

> **`AUTHENTICATION_REESTABLISHMENT_REQUIRED`**

status concept: a restored Atlas is functionally complete for orchestration
and resume, but a fresh interactive login is required before any browser
work can proceed. This is a policy, not a defect — it is the safe and
correct behaviour for machine-local authenticated sessions. (Interactive
login/human-intervention plumbing already exists in
`atlas/browser/intervention.py`; wiring a real re-auth prompt is future
business-logic work and out of Phase 0.95 scope.)

---

## Safe reset guarantees (developer-only)

`atlas/backup/reset.py::safe_reset(purposes, project_root)` clears
*disposable* working directories during development. It is built so an
accidental catastrophe is impossible:

* **Allow-list of purposes only.** Callers pass *purpose names* from a
  hardcoded map (`RESET_PURPOSES`), never arbitrary paths. Today only
  `generated_fixtures` → `fixtures/generated`, `phase09_output` →
  `output/phase09`, `demo_output` → `output/demo`, and `backups` →
  `output/backups` exist. There is **no** purpose for the browser profiles,
  `.venv`, `fixtures/real`, the live `state/` DBs, or any source directory,
  so they simply cannot be selected.
* **Defense-in-depth guard.** Every resolved target is re-checked by
  `_assert_safe_target`, which raises `ResetSafetyError` if the target is,
  contains, or lives inside a protected path
  (`.browser-profile-chrome`, `.browser-profile`, `fixtures/real`, `.venv`,
  `state`, `atlas`, `tests`, `docs`, `config`), equals the project root, or
  resolves outside the project root. Even a future mis-configured purpose
  cannot delete something protected.
* **All-or-nothing.** Every purpose is validated *before* any deletion, so a
  single bad entry aborts the whole operation with nothing deleted.

Proven by `tests/test_phase095_reset_retention.py`, which exercises real
deletion only against a `tmp_path` tree that *mimics* the project layout —
never the real `C:\Atlas` tree — and asserts the protected paths survive
even when explicitly requested.

The immutable fixture at `fixtures/real/Atlas_Jobs_2026-08-13.xlsx`
(SHA-256 `440B9587...B8AC`) is therefore never touched by any backup,
restore, retention, or reset operation.

---

## Recovery runbook (operator)

1. **Assess.** Run `atlas doctor`. If the state/checkpoint DBs are missing
   or fail to open, a restore is warranted.
2. **Pick a good backup.** `atlas backup verify <dir>` on candidate
   backups; choose the newest that reports `PASS`.
3. **Restore to a scratch location first.**
   `atlas restore <backup-dir> --target <scratch-dir>` and confirm status
   `RESTORED`.
4. **Promote.** Stop any Atlas process, then move the restored
   `state/*.sqlite` files into `C:\Atlas\state\` (the safety guard forbids
   restoring *directly* onto the live `state/` dir, which is intentional —
   promote manually after verifying the scratch restore).
5. **Re-authenticate the browser** if browser work is needed
   (`AUTHENTICATION_REESTABLISHMENT_REQUIRED`).
6. **Resume.** `atlas resume --run-id <id>` continues from the restored
   checkpoint without repeating completed work.
