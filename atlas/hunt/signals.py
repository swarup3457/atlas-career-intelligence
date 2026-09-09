"""Deterministic technology-signal matching for lane qualification.

The failed workbook admitted Ruby/Go/PM/SDET jobs into Java lanes because the
old matcher did naive substring containment: ``"react" in "reactive"`` and
``"api" in "..."`` matched anything. This module matches a signal only as a
whole token with alphanumeric boundaries, so:

- ``react`` matches "React Developer" but NOT "reactive programming";
- ``go`` matches "Go microservices" but NOT "google"/"ongoing";
- ``c#`` / ``.net`` / ``asp.net`` / ``node.js`` match their literal forms.

There is no LLM here — this is pure, testable string logic.
"""

from __future__ import annotations

import re
from functools import lru_cache

__all__ = ["signal_present", "find_signals", "normalize_text"]


def normalize_text(text: object) -> str:
    """Lower-case and whitespace-collapse arbitrary (untrusted) job text."""
    if not text:
        return ""
    return " ".join(str(text).strip().lower().split())


@lru_cache(maxsize=4096)
def _pattern_for(signal: str) -> re.Pattern[str]:
    s = signal.strip().lower()
    esc = re.escape(s)
    # Left boundary: the char immediately before must not be alphanumeric, so a
    # signal never matches the tail of a larger token ("c#" not in "abcc#").
    # Right boundary: the char immediately after must not be alphanumeric, so
    # "react" does not match "reactive" and "go" does not match "golang".
    return re.compile(r"(?<![a-z0-9])" + esc + r"(?![a-z0-9])")


def signal_present(text: object, signal: str) -> bool:
    """True when ``signal`` appears as a bounded token in ``text``."""
    if not signal:
        return False
    hay = text if isinstance(text, str) and text == text.lower() else normalize_text(text)
    if not hay:
        return False
    return _pattern_for(signal.strip().lower()).search(hay) is not None


def find_signals(text: object, signals) -> tuple[str, ...]:
    """Return the subset of ``signals`` present in ``text`` (original casing,
    order-preserving, de-duplicated)."""
    hay = normalize_text(text)
    if not hay:
        return ()
    seen: list[str] = []
    for sig in signals or ():
        if sig and _pattern_for(str(sig).strip().lower()).search(hay):
            if sig not in seen:
                seen.append(sig)
    return tuple(seen)
