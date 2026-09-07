"""Atlas bounded raw-evidence store (Phase 1A).

Keeps just enough raw source evidence to diagnose parser/selector problems,
without ever hoarding whole webpages or leaking secrets. Every fragment is:

    * content-addressed by a hash of the ORIGINAL content (stable identity),
    * secret-redacted, and rejected outright if a secret survives redaction,
    * size-capped (a bounded fragment, not the full page),
    * optionally compressed,
    * tagged with a sensitivity marker and a retention expiry.

Raw evidence must NEVER contain credentials, cookies, authorization headers,
browser storage, or secrets. Those are redacted/rejected here.
"""

from __future__ import annotations

import datetime
import hashlib
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from atlas.sources.untrusted import contains_secret, redact_secrets


class EvidenceRejected(ValueError):
    """Raised when content cannot be safely stored (e.g. a secret survived
    redaction)."""


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


@dataclass(frozen=True)
class RawEvidence:
    content_hash: str
    source_instance: str
    content_type: str
    captured_at: str
    original_size: int
    stored_size: int
    truncated: bool
    compressed: bool
    sensitivity: str
    expires_at: Optional[str]
    _payload: Optional[bytes] = field(default=None, repr=False)

    def fragment(self) -> Optional[str]:
        """Return the (redacted, bounded) stored fragment as text, or None
        when only the hash was retained (sensitivity='secret')."""
        if self._payload is None:
            return None
        raw = zlib.decompress(self._payload) if self.compressed else self._payload
        return raw.decode("utf-8", errors="replace")

    def to_dict(self, *, include_fragment: bool = False) -> dict:
        data = {
            "content_hash": self.content_hash,
            "source_instance": self.source_instance,
            "content_type": self.content_type,
            "captured_at": self.captured_at,
            "original_size": self.original_size,
            "stored_size": self.stored_size,
            "truncated": self.truncated,
            "compressed": self.compressed,
            "sensitivity": self.sensitivity,
            "expires_at": self.expires_at,
        }
        if include_fragment:
            data["fragment"] = self.fragment()
        return data


class EvidenceStore:
    """A bounded, content-addressed evidence store (in-memory, with an
    optional filesystem sink for the metadata index)."""

    def __init__(
        self,
        *,
        max_fragment_bytes: int = 8192,
        compress_over_bytes: int = 1024,
        retention_days: int = 14,
    ):
        if max_fragment_bytes < 1:
            raise ValueError("max_fragment_bytes must be >= 1")
        if compress_over_bytes < 0:
            raise ValueError("compress_over_bytes must be >= 0")
        if retention_days < 0:
            raise ValueError("retention_days must be >= 0")
        self.max_fragment_bytes = max_fragment_bytes
        self.compress_over_bytes = compress_over_bytes
        self.retention_days = retention_days
        self._store: dict[str, RawEvidence] = {}

    def build(
        self,
        content: str,
        *,
        source_instance: str,
        content_type: str = "text/html",
        sensitivity: str = "public",
        now: Optional[datetime.datetime] = None,
    ) -> RawEvidence:
        if content is None:
            raise ValueError("content must not be None")
        now = now or _utcnow()
        original_bytes = content.encode("utf-8")
        content_hash = hashlib.sha256(original_bytes).hexdigest()

        redacted = redact_secrets(content)
        if contains_secret(redacted):
            raise EvidenceRejected(
                "content still appears to contain a secret after redaction; refusing to store raw evidence"
            )

        expires_at = None
        if self.retention_days > 0:
            expires_at = (now + datetime.timedelta(days=self.retention_days)).isoformat()

        # sensitivity='secret' → retain hash + metadata only, no payload.
        if sensitivity == "secret":
            return RawEvidence(
                content_hash=content_hash,
                source_instance=source_instance,
                content_type=content_type,
                captured_at=now.isoformat(),
                original_size=len(original_bytes),
                stored_size=0,
                truncated=True,
                compressed=False,
                sensitivity=sensitivity,
                expires_at=expires_at,
                _payload=None,
            )

        fragment_bytes = redacted.encode("utf-8")
        truncated = len(fragment_bytes) > self.max_fragment_bytes
        fragment_bytes = fragment_bytes[: self.max_fragment_bytes]
        compressed = False
        payload = fragment_bytes
        if len(fragment_bytes) >= self.compress_over_bytes and self.compress_over_bytes > 0:
            payload = zlib.compress(fragment_bytes)
            compressed = True

        return RawEvidence(
            content_hash=content_hash,
            source_instance=source_instance,
            content_type=content_type,
            captured_at=now.isoformat(),
            original_size=len(original_bytes),
            stored_size=len(payload),
            truncated=truncated,
            compressed=compressed,
            sensitivity=sensitivity,
            expires_at=expires_at,
            _payload=payload,
        )

    def put(
        self,
        content: str,
        *,
        source_instance: str,
        content_type: str = "text/html",
        sensitivity: str = "public",
        now: Optional[datetime.datetime] = None,
    ) -> str:
        """Store evidence and return its content-hash reference."""
        evidence = self.build(
            content,
            source_instance=source_instance,
            content_type=content_type,
            sensitivity=sensitivity,
            now=now,
        )
        self._store[evidence.content_hash] = evidence
        return evidence.content_hash

    def get(self, ref: str) -> Optional[RawEvidence]:
        return self._store.get(ref)

    def __len__(self) -> int:
        return len(self._store)

    def purge_expired(self, now: Optional[datetime.datetime] = None) -> int:
        now = now or _utcnow()
        now_iso = now.isoformat()
        expired = [h for h, e in self._store.items() if e.expires_at is not None and e.expires_at <= now_iso]
        for h in expired:
            del self._store[h]
        return len(expired)


__all__ = ["EvidenceRejected", "RawEvidence", "EvidenceStore"]
