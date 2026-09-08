"""Deterministic policy fingerprinting + versioning (Phase 1B, build spec 8).

Every loaded policy carries a schema version, a policy version, a
deterministic content fingerprint, and source provenance. A production run
records the policy fingerprint it used, so two runs can be compared and a
run can be tied back to the exact policy that produced it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def fingerprint_payload(payload: Any) -> str:
    """Stable sha256 hex digest of a JSON-serializable policy payload.
    Order-independent for mappings (sorted keys)."""
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def short(fingerprint: str, n: int = 12) -> str:
    return fingerprint[:n]


__all__ = ["fingerprint_payload", "short"]
