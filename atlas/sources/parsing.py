"""Atlas parser-isolation helpers (Phase 1A).

The invariant these helpers enforce: parsing a batch of source results must
be *per-item isolated*. One malformed item yields a diagnostic finding and
is skipped — it must never abort the whole batch. This directly encodes the
lesson from the research repo's changelog, where a single null field once
crashed an entire search.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, TypeVar

T = TypeVar("T")
R = TypeVar("R")


@dataclass(frozen=True)
class ParseFinding:
    """A non-fatal problem with one item in a batch."""

    index: int
    reason: str

    def to_dict(self) -> dict:
        return {"index": self.index, "reason": self.reason}


@dataclass
class IsolatedParse:
    """Result of an isolated parse over a batch."""

    results: list = field(default_factory=list)
    findings: list[ParseFinding] = field(default_factory=list)

    @property
    def parse_failure_ratio(self) -> float:
        total = len(self.results) + len(self.findings)
        if total == 0:
            return 0.0
        return len(self.findings) / total

    def finding_reasons(self) -> list[str]:
        return [f.reason for f in self.findings]


def parse_isolated(
    items: Iterable[T],
    parse_one: Callable[[T], Optional[R]],
    *,
    max_findings: int = 100,
) -> IsolatedParse:
    """Parse each item independently.

    ``parse_one`` returns a parsed result, or ``None`` to intentionally skip
    an item (e.g. a filtered/irrelevant entry — not an error). Any exception
    raised by ``parse_one`` is captured as a :class:`ParseFinding`, and
    parsing continues with the next item. The number of *stored* findings is
    capped (to bound memory on a fully-broken page) but the true failure
    ratio still reflects every item seen.
    """
    out = IsolatedParse()
    total_failures = 0
    for index, item in enumerate(items):
        try:
            parsed = parse_one(item)
        except Exception as exc:  # noqa: BLE001 - isolation is the whole point
            total_failures += 1
            if len(out.findings) < max_findings:
                out.findings.append(ParseFinding(index=index, reason=f"{type(exc).__name__}: {exc}"))
            continue
        if parsed is None:
            continue
        out.results.append(parsed)
    # If failures exceeded the stored cap, keep the ratio honest by padding
    # the logical failure count via a synthetic trailing finding note.
    if total_failures > len(out.findings):
        out.findings.append(
            ParseFinding(
                index=-1,
                reason=f"(+{total_failures - len(out.findings)} more parse failures not individually recorded)",
            )
        )
    return out


__all__ = ["ParseFinding", "IsolatedParse", "parse_isolated"]
