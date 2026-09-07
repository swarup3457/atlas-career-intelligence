"""Sanitized support bundle (Phase 0.95).

``atlas support-bundle`` produces a small, shareable ``.zip`` for
diagnosing a problem WITHOUT leaking anything sensitive. It contains only:

* ``info.json``          — atlas/python/platform versions, config
                            fingerprint, state schema version;
* ``doctor.txt``         — the offline :func:`atlas.health.run_doctor`
                            report;
* ``run_manifest.json``  — the most recent run manifest, if any;
* ``atlas_log_tail.log`` — a redacted tail of the structured log; and
* ``error_summary.txt``  — the error/warning lines from that tail.

It NEVER includes cookies, credentials, tokens, browser storage, resume
files, or the browser profile. The log tail is redacted line-by-line: any
line mentioning a secret-like keyword is dropped entirely, so a
mis-logged secret can never ride along.

The zip is written atomically (``.part`` temp then ``os.replace``) so an
interrupted bundle never leaves a half-written archive at the final path.
"""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path
from typing import Optional

from atlas.backup.clock import Clock, resolve_clock
from atlas.backup.config_export import SENSITIVE_KEY_FRAGMENTS
from atlas.backup.manifest import gather_software_versions
from atlas.backup.timezone_utils import compact_utc_stamp, isoformat_utc
from atlas.config import Settings
from atlas.persistence.sqlite import SCHEMA_VERSION
from atlas.runtime.manifest import compute_config_fingerprint
from atlas.utils.ids import new_ulid

_LOG_TAIL_LINES = 500
_REDACTION_NOTE = "*** line omitted from support bundle (contained a sensitive keyword) ***"

# Member names are fixed and known-safe (no secret-looking path fragments).
INFO_NAME = "info.json"
DOCTOR_NAME = "doctor.txt"
RUN_MANIFEST_NAME = "run_manifest.json"
LOG_TAIL_NAME = "atlas_log_tail.log"
ERROR_SUMMARY_NAME = "error_summary.txt"


def _redact_log_line(line: str) -> Optional[str]:
    """Return the line, or None if it must be dropped for safety."""
    lowered = line.lower()
    if any(fragment in lowered for fragment in SENSITIVE_KEY_FRAGMENTS):
        return None
    return line


def _read_log_tail(log_path: Path, max_lines: int = _LOG_TAIL_LINES) -> list[str]:
    if not log_path.exists():
        return []
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[-max_lines:]


def _sanitize_lines(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        redacted = _redact_log_line(line)
        out.append(redacted if redacted is not None else _REDACTION_NOTE)
    return out


def _most_recent_run_manifest(output_dir: Path) -> Optional[str]:
    if not output_dir.exists():
        return None
    candidates = sorted(
        (p for p in output_dir.glob("run_manifest_*.json") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None
    try:
        return candidates[0].read_text(encoding="utf-8")
    except OSError:
        return None


def create_support_bundle(
    settings: Settings,
    output_dir: Path,
    clock: Optional[Clock] = None,
) -> Path:
    """Create a sanitized diagnostic zip under ``output_dir`` and return its path."""
    from atlas.health import run_doctor  # local import: doctor imports config

    clock = resolve_clock(clock)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    now = clock.utcnow()
    versions = gather_software_versions()
    bundle_name = f"atlas_support_bundle_{compact_utc_stamp(now)}-{new_ulid(now)[:10]}.zip"
    final_path = output_dir / bundle_name
    temp_path = output_dir / (bundle_name + ".part")

    info = {
        "created_at": isoformat_utc(now),
        "atlas_version": versions.get("atlas", "unknown"),
        "python_version": versions.get("python", "unknown"),
        "langgraph_version": versions.get("langgraph", "unknown"),
        "playwright_version": versions.get("playwright", "unknown"),
        "platform": versions.get("platform", "unknown"),
        "state_schema_version": SCHEMA_VERSION,
        "config_fingerprint": compute_config_fingerprint(settings),
    }

    try:
        doctor_text = run_doctor().render()
    except Exception as exc:  # noqa: BLE001 - diagnostics must never crash the bundle
        doctor_text = f"run_doctor() failed: {exc}"

    log_tail = _sanitize_lines(_read_log_tail(settings.logs_dir / "atlas.log"))
    error_lines = [ln for ln in log_tail if any(tag in ln.upper() for tag in ("ERROR", "CRITICAL", "WARNING"))]
    run_manifest_text = _most_recent_run_manifest(settings.output_dir)

    temp_path.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(INFO_NAME, json.dumps(info, indent=2, sort_keys=True))
            zf.writestr(DOCTOR_NAME, doctor_text)
            zf.writestr(LOG_TAIL_NAME, "\n".join(log_tail))
            zf.writestr(ERROR_SUMMARY_NAME, "\n".join(error_lines) or "(no error/warning lines in log tail)")
            if run_manifest_text is not None:
                zf.writestr(RUN_MANIFEST_NAME, run_manifest_text)
        # Validate the temp archive before publishing.
        with zipfile.ZipFile(temp_path, "r") as zf:
            bad = zf.testzip()
            if bad is not None:
                raise RuntimeError(f"support bundle failed integrity check on member {bad}")
        os.replace(temp_path, final_path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise

    return final_path


__all__ = ["create_support_bundle"]
