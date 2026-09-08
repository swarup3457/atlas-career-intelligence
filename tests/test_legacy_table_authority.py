"""Phase 1B.1 — legacy table authority enforcement (build spec 28).

The production runtime must stay on the CANONICAL model and never write the
old per-run legacy tables. Authoritative vs legacy:

    company_registry           (canonical)  vs  companies   (legacy per-run)
    source_instances           (canonical)  vs  sources     (legacy)
    canonical_jobs + raw_...    (canonical)  vs  jobs        (legacy)

See docs/LEGACY_TABLE_AUTHORITY.md.
"""

from __future__ import annotations

import pytest

from atlas.config import load_settings
from atlas.orchestration.production_state import ProductionTerminalState
from atlas.persistence.sqlite import StateStore
from atlas.runtime.production import ProductionSearchRuntime

pytestmark = pytest.mark.integration

_LEGACY_TABLES = ("companies", "sources", "jobs", "job_sources", "company_checks")


def _settings(tmp_path):
    s = load_settings(
        state_db=tmp_path / "state" / "st.sqlite", checkpoint_db=tmp_path / "state" / "cp.sqlite",
        output_dir=tmp_path / "out", logs_dir=tmp_path / "logs", browser_profile=tmp_path / "prof",
        agents_dir=tmp_path / "agents", skills_dir=tmp_path / "skills",
    )
    s.ensure_directories()
    return s


def test_production_runtime_does_not_write_legacy_tables(tmp_path):
    settings = _settings(tmp_path)
    res = ProductionSearchRuntime(settings, "legacy-check").run()
    assert res.terminal_state == ProductionTerminalState.COMPLETE.value

    with StateStore(settings.state_db) as store:
        for table in _LEGACY_TABLES:
            count = store._conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]  # noqa: S608
            assert count == 0, f"production runtime wrote to legacy table {table!r} ({count} rows)"
        # The canonical model IS populated.
        assert store.count_canonical_jobs() > 0
        assert store.count_raw_observations("legacy-check") > 0
        assert len(store.list_coverage("legacy-check")) > 0
        assert store.get_coverage_plan("legacy-check") is not None
        assert len(store.list_source_instances()) > 0
