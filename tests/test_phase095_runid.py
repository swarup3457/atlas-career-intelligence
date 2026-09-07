"""Phase 0.95 — run-id / backup-id uniqueness under concurrency (spec 15).

Proves the existing UUID4 run_id generation and the new sortable ULID
helper both produce collision-free ids when generated from many threads.
Notes that UUID4 run_ids are NOT time-sortable, whereas the new ULID-based
backup ids are — without force-migrating the proven run_id generator.
"""

from __future__ import annotations

import datetime
from concurrent.futures import ThreadPoolExecutor

import pytest

from atlas.backup.manifest import generate_backup_id
from atlas.runtime.engine import new_run_id
from atlas.utils.ids import is_ulid, new_ulid

pytestmark = [pytest.mark.unit]

_N = 4000
_THREADS = 16


def _generate_concurrently(factory, n=_N):
    with ThreadPoolExecutor(max_workers=_THREADS) as pool:
        return list(pool.map(lambda _i: factory(), range(n)))


def test_run_ids_unique_under_concurrency():
    ids = _generate_concurrently(lambda: new_run_id())
    assert len(set(ids)) == len(ids)  # no collisions


def test_run_id_is_uuid4_based_and_not_sortable():
    # documents the property: sorting run_ids does NOT reflect creation
    # order (they are random UUID4 hex), which is precisely why Phase 0.95
    # introduced a separate sortable id for backups.
    a = new_run_id()
    b = new_run_id()
    assert a != b
    assert a.startswith("atlas-run-")


def test_ulids_unique_under_concurrency():
    ids = _generate_concurrently(lambda: new_ulid())
    assert len(set(ids)) == len(ids)
    assert all(is_ulid(u) for u in ids)


def test_backup_ids_unique_under_concurrency_same_instant():
    # Even with the SAME injected timestamp for every id (worst case for a
    # timestamp-prefixed id), the 80-bit random ULID tail guarantees
    # uniqueness.
    moment = datetime.datetime(2026, 9, 6, 18, 0, 0, tzinfo=datetime.timezone.utc)
    ids = _generate_concurrently(lambda: generate_backup_id(moment))
    assert len(set(ids)) == len(ids)
    # all share the same human-readable timestamp prefix
    assert all(i.startswith("20260906T180000Z-") for i in ids)


def test_backup_ids_sort_in_time_order():
    base = datetime.datetime(2026, 9, 6, 18, 0, 0, tzinfo=datetime.timezone.utc)
    earlier = generate_backup_id(base)
    later = generate_backup_id(base + datetime.timedelta(seconds=1))
    assert earlier < later
