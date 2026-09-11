"""Runtime handshake tests (PRODUCTION R1 §5).

``get_run_status`` must expose a stable handshake so a session preflight can
refuse to start a live search against a stale server.
"""

from __future__ import annotations

from pathlib import Path

from atlas.persistence.sqlite import SCHEMA_VERSION, StateStore
from atlas.vscode_hunt.models import RUNTIME_CONTRACT_VERSION
from atlas.vscode_hunt.service import VscodeHuntService, runtime_handshake_is_current


def _service(tmp_path: Path) -> tuple[StateStore, VscodeHuntService]:
    store = StateStore(tmp_path / "state.sqlite")
    return store, VscodeHuntService(store, tmp_path / "out")


def test_status_includes_current_runtime_handshake(tmp_path: Path) -> None:
    store, service = _service(tmp_path)
    with store:
        run_id = service.create_run([{"company_id": "co", "name": "Co", "official_domain": "co.example"}])
        handshake = service.status(run_id)["runtime_handshake"]
    assert handshake["runtime_contract_version"] == RUNTIME_CONTRACT_VERSION
    assert handshake["database_schema_version"] == SCHEMA_VERSION
    assert isinstance(handshake["code_version"], str)
    assert handshake["server_started_at"]
    assert runtime_handshake_is_current(handshake)


def test_stale_runtime_handshake_is_rejected(tmp_path: Path) -> None:
    store, service = _service(tmp_path)
    with store:
        handshake = service.runtime_handshake()
    assert not runtime_handshake_is_current({**handshake, "runtime_contract_version": RUNTIME_CONTRACT_VERSION + 1})
    assert not runtime_handshake_is_current({**handshake, "database_schema_version": int(handshake["database_schema_version"]) - 1})
