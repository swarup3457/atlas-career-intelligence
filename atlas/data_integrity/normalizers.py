"""Deterministic, idempotent value normalizers.

Every normalizer obeys two invariants that the tests enforce:

* **Deterministic** — same input always produces the same output; no
  clocks, randomness, locale, or environment reads.
* **Idempotent** — ``f(f(x)) == f(x)``. Normalizing already-normalized
  data is a no-op, which is what makes re-ingestion safe.

Normalizers return only the value (the caller records which normalizer it
used). They are registered by name in :data:`REGISTRY` so a mapping config
can reference a normalizer purely by string.
"""

from __future__ import annotations

import datetime
import re
import unicodedata
from typing import Any, Callable, Optional

# Control characters except we keep normal spacing collapsed to a single
# space. Excludes tab/newline/carriage-return handling done explicitly.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_MULTI_UNDERSCORE_RE = re.compile(r"_+")


def _to_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        # Avoid "1.0" for whole floats; keep ints stable.
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return normalize_date(value)
    return str(value)


def strip_control(value: Any) -> Any:
    """Remove control characters and normalize unicode to NFC. Idempotent."""
    text = _to_text(value)
    if text is None:
        return None
    text = unicodedata.normalize("NFC", text)
    text = _CONTROL_RE.sub("", text)
    # Normalize CRLF/CR to LF so line endings are deterministic.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def normalize_whitespace(value: Any) -> Any:
    """Collapse internal runs of whitespace to a single space and strip.

    Newlines are treated as whitespace and collapsed. Idempotent.
    """
    text = strip_control(value)
    if text is None:
        return None
    text = _WS_RE.sub(" ", text).strip()
    return text


def normalize_text(value: Any) -> Any:
    """Whitespace-normalized text, preserving case. Empty -> None."""
    text = normalize_whitespace(value)
    if text is None or text == "":
        return None
    return text


def normalize_lower(value: Any) -> Any:
    text = normalize_whitespace(value)
    if text is None or text == "":
        return None
    return text.casefold()


def identity_token(value: Any) -> str:
    """Aggressive canonical token for identity comparison.

    Lowercases (casefold), strips accents, removes punctuation, collapses
    whitespace. Used only for *matching*, never stored as display data.
    Idempotent.
    """
    text = normalize_whitespace(value)
    if text is None:
        return ""
    # Strip accents/diacritics deterministically.
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    lowered = stripped.casefold()
    no_punct = _PUNCT_RE.sub(" ", lowered)
    return _WS_RE.sub(" ", no_punct).strip()


def normalize_company(value: Any) -> Any:
    """Display-form company name: whitespace-normalized, control-stripped.

    (Identity matching uses :func:`identity_token`; this keeps the human
    readable name.) Idempotent.
    """
    return normalize_text(value)


def normalize_url(value: Any) -> Any:
    """Lowercase scheme+host, strip trailing slashes/whitespace, drop
    fragments. Leaves path/query case intact. Idempotent."""
    text = normalize_whitespace(value)
    if text is None or text == "":
        return None
    # Remove surrounding angle brackets sometimes present in exports.
    text = text.strip("<>")
    m = re.match(r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)(?P<rest>.*)$", text)
    if not m:
        return text  # not a URL we recognize; leave as-is (normalized text)
    scheme = m.group("scheme").lower()
    rest = m.group("rest")
    # Split host from the remainder.
    if "/" in rest:
        host, _, tail = rest.partition("/")
        tail = "/" + tail
    else:
        host, tail = rest, ""
    host = host.lower()
    # Drop fragment.
    tail = tail.split("#", 1)[0]
    combined = scheme + host + tail
    # Remove a single trailing slash on the bare-host or path (but keep root).
    if combined.endswith("/") and not combined.endswith("://"):
        combined = combined[:-1]
    return combined


def normalize_domain(value: Any) -> Any:
    """Normalize a bare domain / careers host. Strips any scheme, path,
    query, ``www.`` prefix and lowercases. Idempotent.

    ``https://Jobs.Acme.com/careers`` and ``Jobs.Acme.com`` both become
    ``jobs.acme.com``. Non-domain junk is returned normalized (for
    validation to flag).
    """
    text = normalize_whitespace(value)
    if text is None or text == "":
        return None
    text = text.strip("<>")
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://(?P<rest>.*)$", text)
    if m:
        text = m.group("rest")
    # keep only the host portion.
    host = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    host = host.split("@")[-1]  # drop any userinfo
    host = host.rstrip(".").lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


def normalize_date(value: Any) -> Any:
    """Normalize a date/datetime (or many string forms) to ISO ``YYYY-MM-DD``.

    Datetimes with a zero time component collapse to a date. Unparseable
    strings are returned whitespace-normalized (never guessed). Idempotent.
    """
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        if value.hour == value.minute == value.second == value.microsecond == 0:
            return value.date().isoformat()
        return value.replace(microsecond=0).isoformat(sep=" ")
    if isinstance(value, datetime.date):
        return value.isoformat()
    text = normalize_whitespace(value)
    if text is None or text == "":
        return None
    # Try a fixed, ordered set of explicit formats (deterministic).
    candidates = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%d %b %Y",
        "%d %B %Y",
        "%b %d, %Y",
        "%B %d, %Y",
    )
    for fmt in candidates:
        try:
            dt = datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
        if dt.hour == dt.minute == dt.second == 0 and "%H" not in fmt:
            return dt.date().isoformat()
        return dt.date().isoformat() if dt.time() == datetime.time(0, 0) else dt.replace(microsecond=0).isoformat(sep=" ")
    return text  # unparseable: keep normalized text, let validation flag it


def normalize_int(value: Any) -> Any:
    """Parse an integer from int/float/str. Returns None if not an integer.

    Non-integer input is left to validation to flag; this returns the
    original normalized text so nothing is silently corrupted. Idempotent
    for already-int values.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    text = normalize_whitespace(value)
    if text is None or text == "":
        return None
    if re.fullmatch(r"[+-]?\d+", text):
        return int(text)
    if re.fullmatch(r"[+-]?\d+\.0+", text):
        return int(float(text))
    return text  # keep as text; NumericRule will flag it


def normalize_score(value: Any) -> Any:
    """Normalize a match-score. Returns an int/float when numeric, else the
    normalized text (for validation to catch). Idempotent."""
    n = normalize_int(value)
    if isinstance(n, int):
        return n
    text = normalize_whitespace(value)
    if text is None or text == "":
        return None
    # Percent form "86%" -> 86
    m = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)\s*%?", text)
    if m:
        num = float(m.group(1))
        return int(num) if num.is_integer() else num
    return text


_TRUE = {"true", "yes", "y", "1", "on", "t"}
_FALSE = {"false", "no", "n", "0", "off", "f"}


def normalize_bool(value: Any) -> Any:
    """Normalize boolean-ish inputs to Python bool, else normalized text.
    Idempotent."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = normalize_lower(value)
    if text is None or text == "":
        return None
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return normalize_text(value)


def header_key(value: Any) -> str:
    """Canonical key for a column header / sheet name for alias matching.

    Case-insensitive, punctuation/underscore/whitespace-insensitive so
    ``"Company Name"``, ``"company_name"`` and ``"COMPANY-NAME"`` all match.
    Idempotent.
    """
    token = identity_token(value)
    return _MULTI_UNDERSCORE_RE.sub("_", token.replace(" ", "_")).strip("_")


REGISTRY: dict[str, Callable[[Any], Any]] = {
    "identity": lambda v: v,
    "strip_control": strip_control,
    "whitespace": normalize_whitespace,
    "text": normalize_text,
    "lower": normalize_lower,
    "company": normalize_company,
    "url": normalize_url,
    "domain": normalize_domain,
    "date": normalize_date,
    "int": normalize_int,
    "score": normalize_score,
    "bool": normalize_bool,
    "token": identity_token,
}


def apply_normalizer(name: str, value: Any) -> Any:
    fn = REGISTRY.get(name)
    if fn is None:
        raise KeyError(f"Unknown normalizer '{name}'. Known: {sorted(REGISTRY)}")
    return fn(value)
