"""Phase 1A.5: deterministic offline performance smoke (reports timings)."""

from __future__ import annotations

import time

import pytest

from atlas.company.discovery import register_employer
from atlas.company.models import CompanyObservation, DiscoveryMethod
from atlas.company.registry import CompanyRegistry
from atlas.persistence.sqlite import StateStore

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_C = DiscoveryMethod.CONFIRMED_IDENTITY
_N = 1000


def _obs(i: int) -> CompanyObservation:
    return CompanyObservation(
        name=f"Company {i}", official_domain=f"company{i}.example",
        careers_url=f"https://boards.greenhouse.io/company{i}", method=_C,
        aliases=(f"Company {i} Inc", f"Co{i} Global"),
    )


def test_company_registry_scale(tmp_path):
    with StateStore(tmp_path / "s.sqlite") as store:
        reg = CompanyRegistry(store)

        start = time.monotonic()
        for i in range(_N):
            register_employer(reg, _obs(i))
        register_elapsed = time.monotonic() - start
        print(f"[perf] register {_N} companies (+aliases +source) = {register_elapsed:.3f}s")
        assert store.count_companies() == _N
        assert store.count_source_relationships() == _N

        start = time.monotonic()
        for i in range(_N):
            assert reg.find_company(f"Company {i}", f"company{i}.example") is not None
        lookup_elapsed = time.monotonic() - start
        print(f"[perf] domain lookup x{_N} = {lookup_elapsed:.3f}s")

        start = time.monotonic()
        for i in range(_N):
            assert reg.find_company(f"Co{i} Global") is not None  # alias resolution
        alias_elapsed = time.monotonic() - start
        print(f"[perf] alias resolution x{_N} = {alias_elapsed:.3f}s")

        start = time.monotonic()
        for i in range(_N):
            register_employer(reg, _obs(i))  # idempotent rediscovery
        rediscover_elapsed = time.monotonic() - start
        print(f"[perf] idempotent rediscovery x{_N} = {rediscover_elapsed:.3f}s")
        assert store.count_companies() == _N  # no growth

        assert register_elapsed < 30.0
        assert lookup_elapsed < 10.0
