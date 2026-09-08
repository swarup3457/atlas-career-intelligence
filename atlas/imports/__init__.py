"""Atlas Workspace-import accounting (Phase 1B).

This package records HOW every file in the private Workspace Agent import
package (``C:\\Atlas-Agent-Import``) was reviewed and migrated into the
Atlas ``local-v2`` architecture. It contains ONLY non-sensitive accounting
metadata (relative paths, sha256 digests, decisions) — never candidate PII
and never the raw imported content. See :mod:`atlas.imports.migration`.
"""

from atlas.imports.migration import (
    Decision,
    MigrationRecord,
    MIGRATION_MATRIX,
    PrivacyClass,
    matrix_as_dicts,
    record_for,
)

__all__ = [
    "Decision",
    "PrivacyClass",
    "MigrationRecord",
    "MIGRATION_MATRIX",
    "matrix_as_dicts",
    "record_for",
]
