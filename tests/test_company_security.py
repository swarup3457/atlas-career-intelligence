"""Phase 1A.5: security regressions for the company/source foundation."""

from __future__ import annotations

from pathlib import Path

import pytest

from atlas.company.discovery import register_employer
from atlas.company.models import CompanyObservation, DiscoveryMethod
from atlas.company.registry import CompanyRegistry
from atlas.persistence.sqlite import StateStore

pytestmark = pytest.mark.unit

_COMPANY_DIR = Path(__file__).resolve().parents[1] / "atlas" / "company"
_RESEARCH_TOKENS = ("Atlas-Research", "ai-job-search", "ai_job_search", "wellfound_autoApply")
_FETCH_TOKENS = ("urlopen(", "urllib.request", "requests.", "httpx", "aiohttp", "socket.socket", "http.client")
_EXEC_TOKENS = ("eval(", "exec(", "os.system(", "subprocess")
_APPLY_TOKENS = ("submit_application", "auto_apply", "click_apply", "fill_application")


def _company_py() -> list[Path]:
    return [p for p in _COMPANY_DIR.rglob("*.py") if "__pycache__" not in p.parts]


def test_no_job_posting_can_register_official_domain(tmp_path):
    with StateStore(tmp_path / "s.sqlite") as store:
        reg = CompanyRegistry(store)
        result = register_employer(reg, CompanyObservation(
            name="Victim Co", official_domain="attacker.example",
            careers_url="https://attacker.example/evil", method=DiscoveryMethod.UNTRUSTED_POSTING))
        # posting-derived domain/source must never be registered
        assert result.company.official_domain is None
        assert result.source_registered is False
        assert store.count_source_relationships() == 0


def test_no_network_fetch_in_company_layer():
    offenders = []
    for path in _company_py():
        text = path.read_text(encoding="utf-8")
        for token in _FETCH_TOKENS:
            if token in text:
                offenders.append(f"{path.name}: {token}")
    assert not offenders, f"network fetch primitives in company layer: {offenders}"


def test_no_dynamic_execution_in_company_layer():
    offenders = []
    for path in _company_py():
        text = path.read_text(encoding="utf-8")
        for token in _EXEC_TOKENS:
            if token in text:
                offenders.append(f"{path.name}: {token}")
    assert not offenders, f"dynamic execution primitives in company layer: {offenders}"


def test_no_research_repo_or_auto_apply_in_company_layer():
    offenders = []
    for path in _company_py():
        text = path.read_text(encoding="utf-8")
        for token in _RESEARCH_TOKENS + _APPLY_TOKENS:
            if token in text:
                offenders.append(f"{path.name}: {token}")
    assert not offenders, f"forbidden references in company layer: {offenders}"


def test_company_layer_stores_no_credentials(tmp_path):
    # Company observations carry no credential fields; discovery never
    # persists secrets. A smoke registration writes only identity/provenance.
    with StateStore(tmp_path / "s.sqlite") as store:
        reg = CompanyRegistry(store)
        reg.register_company(CompanyObservation(name="Acme", official_domain="acme.com"))
        row = store.get_company(reg.list_companies()[0].company_id)
        blob = " ".join(str(row[k]) for k in row.keys()).lower()
        for secret_word in ("password", "token", "secret", "authorization", "cookie"):
            assert secret_word not in blob
