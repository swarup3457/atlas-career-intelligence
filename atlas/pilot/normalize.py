"""Canonical source-text normalization for the agentic pilot (V4 architecture s.7).

The V3 pilot stripped only four named HTML entities, so numeric entities such as
``4&#43; years`` survived and the experience parser never saw the ``+`` — a hard
``4+`` minimum fell through as *ambiguous* and therefore *eligible*. This module
is the single canonical normalizer that MUST run before any experience / role /
requirement parsing:

* ``html.unescape`` (named **and** numeric entities, incl. ``&#43;`` / ``&#8211;``);
* Unicode ``NFKC``;
* en/em/figure dashes and the Unicode minus normalized to an ASCII hyphen-minus;
* non-breaking and other Unicode spaces normalized to an ordinary space;
* curly quotes / ellipsis normalized;
* whitespace collapsed **while preserving paragraph / list / heading boundaries**.

:func:`split_sections` then separates *required / minimum / basic* qualifications
from *preferred / nice-to-have* qualifications so a preferred ``5+`` is never read
as a mandatory minimum (audit root cause 6, prompt s.7).

Pure, deterministic, dependency-light: no LLM, no network, no Atlas imports.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field

__all__ = [
    "normalize_source_text",
    "split_sections",
    "SectionedText",
    "MANDATORY_SECTION_HEADERS",
    "PREFERRED_SECTION_HEADERS",
]

# Any dash-like code point -> ASCII hyphen-minus so "8–12+" parses as a range.
_DASH_RE = re.compile(r"[\u2010\u2011\u2012\u2013\u2014\u2015\u2212\u2043\uFE58\uFE63\uFF0D]")
# Any non-breaking / exotic Unicode space -> ordinary space (NFKC covers most; be explicit).
_SPACE_RE = re.compile(r"[\u00a0\u1680\u2000-\u200a\u202f\u205f\u2060\u3000\ufeff]")
_CURLY_MAP = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2033": '"', "\u2032": "'",
    "\u2026": "...",
}
_CURLY_RE = re.compile("|".join(re.escape(k) for k in _CURLY_MAP))

# Block-level tags whose boundary is a real paragraph/list/heading break.
_BLOCK_TAGS = (
    "p", "div", "li", "ul", "ol", "tr", "table", "section", "article", "header",
    "footer", "h1", "h2", "h3", "h4", "h5", "h6", "br", "hr", "dt", "dd",
    "blockquote", "pre",
)
_BLOCK_OPEN_CLOSE_RE = re.compile(
    r"</?(?:" + "|".join(_BLOCK_TAGS) + r")\b[^>]*>", re.I
)
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_HTML_HINT_RE = re.compile(r"<[a-zA-Z/][^>]*>")
_INLINE_WS_RE = re.compile(r"[ \t\f\v]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")


def _apply_char_maps(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = _DASH_RE.sub("-", text)
    text = _SPACE_RE.sub(" ", text)
    text = _CURLY_RE.sub(lambda m: _CURLY_MAP[m.group(0)], text)
    return text


def _collapse_ws_preserving_lines(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_INLINE_WS_RE.sub(" ", ln).strip() for ln in text.split("\n")]
    text = "\n".join(lines)
    text = _MULTI_NL_RE.sub("\n\n", text)
    return text.strip()


def normalize_source_text(raw: object, *, is_html: object = None, collapse: bool = True) -> str:
    """Return the canonical, decoded, normalized form of ``raw``.

    ``html.unescape`` is ALWAYS applied (numeric entities appear even in
    otherwise-plain Workday text). When the input looks like HTML the block-level
    structure is converted to newlines and tags stripped BEFORE unescaping, so an
    entity-encoded ``&lt;`` in the copy can never be mistaken for a tag.
    """
    if raw is None:
        return ""
    s = str(raw)
    if not s.strip():
        return ""

    detected_html = bool(_HTML_HINT_RE.search(s)) if is_html is None else bool(is_html)
    if detected_html:
        s = _SCRIPT_STYLE_RE.sub(" ", s)
        s = _BLOCK_OPEN_CLOSE_RE.sub("\n", s)
        s = _TAG_RE.sub(" ", s)

    s = html.unescape(s)
    s = _apply_char_maps(s)
    if collapse:
        s = _collapse_ws_preserving_lines(s)
    return s


# ---------------------------------------------------------------------------
# Section-aware splitting (prompt s.7)
# ---------------------------------------------------------------------------
MANDATORY_SECTION_HEADERS: tuple[str, ...] = (
    "minimum qualifications",
    "basic qualifications",
    "required qualifications",
    "required skills",
    "requirements",
    "must have",
    "must haves",
    "experience you'll need",
    "experience you will need",
    "what you'll need",
    "what you will need",
    "what we're looking for",
    "what we are looking for",
    "who you are",
    "key skills",
    "essential skills",
    "essential qualifications",
    "qualifications",
    "skills and experience",
    "your profile",
)
PREFERRED_SECTION_HEADERS: tuple[str, ...] = (
    "preferred qualifications",
    "preferred skills",
    "preferred experience",
    "nice to have",
    "nice-to-have",
    "nice to haves",
    "good to have",
    "good-to-have",
    "what would be great to have",
    "would be great to have",
    "bonus points",
    "bonus",
    "desirable",
    "desired skills",
    "pluses",
    "added advantage",
    "great to have",
)

# Longer headers first so "preferred qualifications" wins over "qualifications".
_HEADER_LOOKUP: tuple[tuple[str, str], ...] = tuple(
    sorted(
        [(h, "PREFERRED") for h in PREFERRED_SECTION_HEADERS]
        + [(h, "MANDATORY") for h in MANDATORY_SECTION_HEADERS],
        key=lambda kv: len(kv[0]),
        reverse=True,
    )
)


@dataclass
class SectionedText:
    """Sectioned view of a normalized description."""

    overview: str = ""
    sections: list[tuple[str, str, str]] = field(default_factory=list)  # (bucket, header, body)

    @property
    def mandatory_text(self) -> str:
        parts = [self.overview] + [body for bucket, _h, body in self.sections if bucket == "MANDATORY"]
        return "\n".join(p for p in parts if p).strip()

    @property
    def preferred_text(self) -> str:
        parts = [body for bucket, _h, body in self.sections if bucket == "PREFERRED"]
        return "\n".join(p for p in parts if p).strip()

    def to_dict(self) -> dict:
        return {
            "overview": self.overview,
            "sections": [{"bucket": b, "header": h, "body": body} for b, h, body in self.sections],
        }


def _match_header(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or len(stripped) > 70:
        return None
    low = stripped.lower().rstrip(":.- \t").strip()
    # A header line is short and dominated by the header phrase itself.
    for phrase, bucket in _HEADER_LOOKUP:
        if low == phrase:
            return bucket, phrase
        if low.startswith(phrase) and len(low) - len(phrase) <= 3:
            return bucket, phrase
        # "Preferred Qualifications (nice to have)" style headers.
        if low.startswith(phrase) and len(low) <= len(phrase) + 22 and low[len(phrase):].lstrip()[:1] in ("(", "/", "-", "\u2013"):
            return bucket, phrase
    return None


def split_sections(text: object) -> SectionedText:
    """Split normalized description text into overview + labeled mandatory /
    preferred sections. Text before the first recognized header is the overview
    (treated as mandatory context)."""
    norm = normalize_source_text(text)
    result = SectionedText()
    if not norm:
        return result
    lines = norm.split("\n")
    cur_bucket: str | None = None
    cur_header = ""
    overview: list[str] = []
    body: list[str] = []

    def _flush() -> None:
        nonlocal body, cur_bucket, cur_header
        if cur_bucket is not None:
            result.sections.append((cur_bucket, cur_header, "\n".join(body).strip()))
        body = []

    for line in lines:
        hit = _match_header(line)
        if hit is not None:
            _flush()
            cur_bucket, cur_header = hit
            continue
        if cur_bucket is None:
            overview.append(line)
        else:
            body.append(line)
    _flush()
    result.overview = "\n".join(overview).strip()
    return result
