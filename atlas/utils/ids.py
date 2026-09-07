"""Sortable, collision-resistant identifiers (Phase 0.95).

Atlas's existing ``run_id`` generation (``atlas.runtime.engine.new_run_id``)
is UUID4-based: unique but NOT lexicographically sortable by creation
time. Phase 0.95 backups benefit from an id that sorts in creation order
(so a directory listing is chronological and retention can order backups
without parsing every manifest), so this module adds a small ULID-style
helper.

A ULID is 128 bits = a 48-bit millisecond timestamp followed by 80 bits
of randomness, encoded as 26 Crockford base32 characters. Because the
timestamp is the high-order component and base32 preserves byte order,
ULIDs are lexicographically sortable by time while still being globally
unique in practice (80 random bits per millisecond).

This helper is used ONLY by new Phase 0.95 code (backup ids). It does not
touch or migrate the proven UUID4 run_id generation.
"""

from __future__ import annotations

import datetime
import os
import secrets
from typing import Optional

# Crockford's base32 alphabet (no I, L, O, U to avoid ambiguity).
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ENCODED_TIME_LEN = 10  # 48 bits -> 10 base32 chars
_ENCODED_RANDOM_LEN = 16  # 80 bits -> 16 base32 chars
_ULID_LEN = _ENCODED_TIME_LEN + _ENCODED_RANDOM_LEN  # 26


def _encode(value: int, length: int) -> str:
    """Encode ``value`` as ``length`` Crockford base32 chars (big-endian)."""
    chars = ["0"] * length
    for i in range(length - 1, -1, -1):
        value, rem = divmod(value, 32)
        chars[i] = _CROCKFORD[rem]
    if value:
        raise ValueError("value too large for the requested encoded length")
    return "".join(chars)


def new_ulid(moment: Optional[datetime.datetime] = None) -> str:
    """Return a 26-char, lexicographically sortable ULID string.

    ``moment`` (a timezone-aware datetime) sets the timestamp component;
    when omitted, the current UTC time is used. Randomness comes from the
    OS CSPRNG (:func:`secrets.token_bytes`), giving 80 bits of entropy so
    collisions are astronomically unlikely even for large concurrent
    batches created within the same millisecond.
    """
    if moment is None:
        moment = datetime.datetime.now(datetime.timezone.utc)
    elif moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    ms = int(moment.timestamp() * 1000)
    if ms < 0:
        raise ValueError("ULID timestamp must not be before the Unix epoch")
    rand = int.from_bytes(secrets.token_bytes(10), "big")  # 80 bits
    return _encode(ms, _ENCODED_TIME_LEN) + _encode(rand, _ENCODED_RANDOM_LEN)


def is_ulid(value: str) -> bool:
    """True if ``value`` looks like a well-formed ULID (length + alphabet)."""
    if len(value) != _ULID_LEN:
        return False
    return all(ch in _CROCKFORD for ch in value.upper())


__all__ = ["new_ulid", "is_ulid"]
