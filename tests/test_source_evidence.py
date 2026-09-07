"""Phase 1A: bounded raw-evidence store tests."""

from __future__ import annotations

import datetime

import pytest

from atlas.sources.evidence import EvidenceRejected, EvidenceStore

pytestmark = pytest.mark.unit


def test_hash_stable_and_addressable():
    store = EvidenceStore()
    ref1 = store.put("<html>job</html>", source_instance="s")
    ref2 = store.put("<html>job</html>", source_instance="s")
    assert ref1 == ref2
    assert store.get(ref1) is not None


def test_truncation_bounds_fragment():
    store = EvidenceStore(max_fragment_bytes=100, compress_over_bytes=0)
    ref = store.put("x" * 500, source_instance="s")
    ev = store.get(ref)
    assert ev.truncated is True
    assert len(ev.fragment()) <= 100
    assert ev.original_size == 500


def test_compression_roundtrip():
    store = EvidenceStore(max_fragment_bytes=100000, compress_over_bytes=1024)
    body = "safe content " * 200  # > 1024 bytes, no secrets
    ref = store.put(body, source_instance="s")
    ev = store.get(ref)
    assert ev.compressed is True
    assert ev.fragment() == body


def test_secret_is_redacted_not_stored():
    store = EvidenceStore()
    token = "ghp_" + "a" * 36
    ref = store.put(f"apply here token {token} end", source_instance="s")
    fragment = store.get(ref).fragment()
    assert token not in fragment
    assert "REDACTED" in fragment


def test_secret_rejected_when_redaction_bypassed(monkeypatch):
    import atlas.sources.evidence as mod

    monkeypatch.setattr(mod, "redact_secrets", lambda text: text)  # simulate redaction miss
    store = EvidenceStore()
    with pytest.raises(EvidenceRejected):
        store.put("Authorization: Bearer sk-secret-token-value", source_instance="s")


def test_sensitivity_secret_keeps_hash_only():
    store = EvidenceStore()
    ref = store.put("some body", source_instance="s", sensitivity="secret")
    ev = store.get(ref)
    assert ev.fragment() is None
    assert ev.stored_size == 0


def test_retention_purge():
    store = EvidenceStore(retention_days=1)
    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    store.put("body", source_instance="s", now=now)
    assert len(store) == 1
    later = now + datetime.timedelta(days=2)
    assert store.purge_expired(later) == 1
    assert len(store) == 0
