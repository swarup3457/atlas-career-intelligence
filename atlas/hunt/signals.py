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

__all__ = ["signal_present", "find_signals", "normalize_text", "strip_alternative_language_enumerations"]


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


# ---------------------------------------------------------------------------
# Alternative-language enumerations (prompt s.3.3, s.8)
# ---------------------------------------------------------------------------
# A single programming language token. Short tokens (go/r/c) carry a right word
# boundary so "go" never matches "google". Longer names first for greediness.
_LANG = (
    r"(?:python|typescript|javascript|node\.?js|golang|java(?:script)?|scala|kotlin|"
    r"swift|ruby|rust|elixir|haskell|perl|dart|php|c\+\+|c#|go(?![a-z])|r(?![a-z])|c(?![a-z+#]))"
)
# "X, Y, Java, or Z" — 3+ languages joined by commas and a terminal "or" is an
# alternatives list ("use any one"), NOT a mandatory Java/JVM backend.
_ALT_LANG_LIST_RE = re.compile(
    r"(?<![a-z0-9])(?:one of\s+|such as\s+|like\s+|using\s+|e\.g\.,?\s+|for example,?\s+)?"
    r"(" + _LANG + r"(?:\s*,\s*" + _LANG + r"){1,}\s*,?\s*or\s+" + _LANG + r")(?![a-z0-9])",
    re.I,
)


def strip_alternative_language_enumerations(text: object) -> str:
    """Neutralize alternative-language enumerations of 3+ languages so no single
    language inside "Python, TypeScript, Java, or Go" anchors a lane. A genuine
    Java/Spring mention elsewhere in the same posting is untouched."""
    if not text:
        return "" if text is None else str(text)
    s = str(text)

    def _repl(m: re.Match[str]) -> str:
        span = m.group(0)
        if len(re.findall(_LANG, span, re.I)) >= 3:
            return " alternative programming languages "
        return span

    return _ALT_LANG_LIST_RE.sub(_repl, s)
