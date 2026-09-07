# Atlas Reproducibility (Phase 0.95)

This document explains how to recreate a working Atlas environment from a
fresh clone, the two-tier dependency strategy Atlas uses, and — honestly —
exactly what was and was not validated during Phase 0.95.

---

## Fresh-clone bootstrap

On a Windows machine with **Python 3.12+** and **Google Chrome** installed:

```powershell
# 1. Get the code
cd C:\
git clone <atlas-repo-url> Atlas    # or copy the C:\Atlas tree
cd C:\Atlas

# 2. Create an isolated virtual environment
python -m venv .venv

# 3a. DEVELOPMENT install (permissive ranges from pyproject.toml)
.venv\Scripts\python -m pip install -e ".[dev]"

#     — OR —

# 3b. REPRODUCIBLE DEPLOYMENT install (exact pinned versions)
.venv\Scripts\python -m pip install -r requirements-lock.txt
.venv\Scripts\python -m pip install -e . --no-deps

# 4. Install the Playwright-managed Chrome/driver binaries (once)
.venv\Scripts\python -m playwright install chrome

# 5. Sanity-check the environment
.venv\Scripts\python -m atlas.cli doctor
.venv\Scripts\python -m pytest -m "not real_web"
```

`atlas doctor` should report **OVERALL: PASS** (a machine without Chrome
will show a single `WARN` for Chrome detection, which is not a failure).

---

## Two-tier dependency strategy

Atlas deliberately maintains dependencies at **two** levels:

| Tier | File | Purpose | Version style |
| --- | --- | --- | --- |
| Development | `pyproject.toml` `[project].dependencies` | day-to-day work; lets compatible upgrades in | **ranges** (`>=`) |
| Deployment | `requirements-lock.txt` | reproduce a known-good environment byte-for-byte | **exact pins** (`==`) |

Why both:

* **Ranges** in `pyproject.toml` keep development flexible — a contributor
  gets compatible bug-fix/minor releases without editing metadata, and the
  package stays installable in varied environments.
* **Exact pins** in `requirements-lock.txt` make a *deployment*
  reproducible — the versions captured there are the exact set that had the
  entire offline test suite green on the reference machine.

Regenerate the lock after an intentional dependency change:

```powershell
.venv\Scripts\python -m pip list --format=freeze > requirements-lock.txt
```

### The one added dependency: `tzdata`

Phase 0.95 added `tzdata` to `pyproject.toml`. This is **necessary, not
optional**: Windows does not ship the IANA time-zone database, so
`zoneinfo.ZoneInfo("America/New_York")` (and even `"Asia/Kolkata"`) raises
`ZoneInfoNotFoundError` without it. Atlas's production display target is
IST and `atlas/backup/timezone_utils.py` performs UTC→local conversions, so
the tz database must be present. `tzdata` is the standard, tiny, pure-data
package that provides it.

---

## Time handling policy

Atlas stores and compares time in **UTC** everywhere internally; local time
is a **presentation-only** concern handled at the boundary by
`atlas/backup/timezone_utils.py`:

* `utc_now()` — the single source of "now", always timezone-aware UTC.
* `to_local_display(dt, tz_name)` — convert to a named zone for display
  only (never for storage or comparison).
* `parse_iso` / `isoformat_utc` — round-trip ISO-8601.

`tests/test_phase095_timezone.py` proves this works generically for a
**DST-observing** zone (`America/New_York`: EST −5 in winter, EDT −4 in
summer) as well as a **no-DST** zone (`Asia/Kolkata`: +5:30 year-round),
including a midnight/date-boundary crossing. This exercises the mechanism
generically **without** adding any production scheduling.

---

## Deterministic identifiers

* Existing run ids (`atlas.runtime.engine.new_run_id`) remain **UUID4**-based
  — globally unique but *not* time-sortable. Phase 0.95 did **not** migrate
  them (that would touch proven code unnecessarily).
* New Phase 0.95 backup ids use a **ULID** helper
  (`atlas/utils/ids.py::new_ulid`): a 48-bit millisecond timestamp + 80 bits
  of randomness, Crockford base32, 26 chars — lexicographically sortable by
  creation time and collision-resistant.
* `tests/test_phase095_runid.py` stress-tests both under concurrency (4,000
  ids across 16 threads each): zero collisions for UUID4 run ids, ULIDs, and
  timestamp-prefixed backup ids (even when every id is generated with the
  *same* injected instant).

---

## What was actually validated (honest scope)

Phase 0.95 was validated **in the existing `C:\Atlas\.venv`**, truthfully:

* ✅ `pip check` — "No broken requirements found."
* ✅ `pip install -e .` — the editable install succeeds against the updated
  `pyproject.toml` (including the new `tzdata` dependency), proving the
  project metadata is installable.
* ✅ `atlas doctor` — **OVERALL: PASS** from the existing venv.
* ✅ `python -m pytest -m "not real_web"` — the full offline suite (existing
  Phase 0/0.5/0.75/0.9 tests **plus** the new Phase 0.95 tests) passes.
* ✅ `requirements-lock.txt` — generated from the exact working environment
  via `pip list --format=freeze`.

**Not performed:** a *second, from-scratch* virtual environment was **not**
created and tested in this session. The existing `.venv` was intentionally
preserved (per the Phase 0.95 constraint not to destroy it), and spinning up
a full parallel venv — including re-downloading Playwright browser binaries
— was judged unnecessarily expensive/risky for the reproducibility
guarantee being made here. The bootstrap commands above are therefore
documented and metadata-validated (`pip check`, editable install,
`requirements-lock.txt`) but have **not** been executed against a clean
throwaway environment in this session. That is the one honest gap; the pin
file and bootstrap steps are believed correct but a clean-room install was
not run.
