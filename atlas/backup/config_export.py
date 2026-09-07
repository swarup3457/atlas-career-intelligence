"""Sanitized configuration export (Phase 0.95).

A backup must capture *enough* configuration to understand and reproduce a
run, but must NEVER capture secrets. This module exports:

1. The resolved :class:`~atlas.config.Settings` (Path values stringified so
   the export is plain JSON), and
2. The raw contents of ``config/default.yaml`` and, if present,
   ``config/local.yaml``,

with every value whose *key name* looks sensitive replaced by
``***REDACTED***``. Redaction is applied recursively to nested mappings and
lists, and to the config-file contents too (not just the known Settings
fields) — because a future config key might be sensitively named, and we
never want a checked-in-by-mistake token to leak into a backup.

The redaction rule is name-based (case-insensitive substring match), which
is the same defensive posture used by :mod:`atlas.utils.logging` and
:func:`atlas.runtime.manifest.compute_config_fingerprint`.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import yaml

from atlas.config import Settings

REDACTED = "***REDACTED***"

# Any key containing one of these fragments (case-insensitive) is redacted.
# The first six are mandated by the Phase 0.95 brief; the rest are common
# aliases kept in sync with atlas.utils.logging for defense in depth.
SENSITIVE_KEY_FRAGMENTS = (
    "password",
    "token",
    "cookie",
    "authorization",
    "secret",
    "credential",
    "passwd",
    "api_key",
    "apikey",
    "auth",
)


def is_sensitive_key(key: str) -> bool:
    """True if ``key``'s name suggests it holds a secret value."""
    lowered = str(key).lower()
    return any(fragment in lowered for fragment in SENSITIVE_KEY_FRAGMENTS)


def redact_structure(value: Any) -> Any:
    """Recursively redact sensitively-named keys inside mappings/lists.

    Scalars are returned unchanged; ``Path`` values are stringified so the
    result is JSON-serializable.
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if is_sensitive_key(k):
                out[k] = REDACTED
            else:
                out[k] = redact_structure(v)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_structure(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return value


def export_settings(settings: Settings) -> dict[str, Any]:
    """Export Settings as a redacted, JSON-safe dict.

    ``Path`` fields become strings; sensitively-named fields (there are
    none today, but a future field could be added) are redacted.
    """
    raw: dict[str, Any] = {}
    for f in dataclasses.fields(settings):
        raw[f.name] = getattr(settings, f.name)
    return redact_structure(raw)


def export_config_files(config_dir: Path) -> dict[str, Any]:
    """Export the raw config YAML files as redacted, JSON-safe structures.

    Returns a mapping ``{filename: parsed-and-redacted-content}`` for
    ``default.yaml`` and ``local.yaml`` (only those that exist). A file
    that fails to parse is recorded as an error entry rather than raising,
    so an operator's malformed local override never breaks a backup.
    """
    config_dir = Path(config_dir)
    result: dict[str, Any] = {}
    for name in ("default.yaml", "local.yaml"):
        path = config_dir / name
        if not path.exists():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            result[name] = {"__error__": f"could not parse: {exc}"}
            continue
        result[name] = redact_structure(data)
    return result


def build_config_export(settings: Settings, config_dir: Path) -> dict[str, Any]:
    """Assemble the full sanitized config export payload."""
    return {
        "settings": export_settings(settings),
        "config_files": export_config_files(config_dir),
    }


__all__ = [
    "REDACTED",
    "SENSITIVE_KEY_FRAGMENTS",
    "is_sensitive_key",
    "redact_structure",
    "export_settings",
    "export_config_files",
    "build_config_export",
]
