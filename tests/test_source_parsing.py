"""Phase 1A: parser-isolation tests (one bad item never kills the batch)."""

from __future__ import annotations

import pytest

from atlas.sources.parsing import parse_isolated

pytestmark = pytest.mark.unit


def _parse(item):
    if item == "boom":
        raise ValueError("malformed card")
    if item == "skip":
        return None
    return {"ok": item}


def test_one_bad_item_does_not_kill_batch():
    result = parse_isolated(["a", "boom", "b"], _parse)
    assert len(result.results) == 2
    assert len(result.findings) == 1
    assert "malformed card" in result.findings[0].reason


def test_none_is_skipped_not_an_error():
    result = parse_isolated(["a", "skip", "b"], _parse)
    assert len(result.results) == 2
    assert len(result.findings) == 0


def test_parse_failure_ratio():
    result = parse_isolated(["a", "boom", "boom", "b"], _parse)
    assert result.parse_failure_ratio == pytest.approx(0.5)


def test_findings_capped_but_ratio_stays_honest():
    items = ["boom"] * 250
    result = parse_isolated(items, _parse, max_findings=10)
    # 10 individually recorded + 1 synthetic summary finding.
    assert len(result.findings) == 11
    assert result.findings[-1].index == -1
    assert "more parse failures" in result.findings[-1].reason
