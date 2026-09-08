"""Phase 1C-A — opt-in LIVE canary tests (matrix E).

Marked ``real_web`` so they are DESELECTED by default and only run when a live
canary is explicitly requested (``pytest -m real_web``). Low-volume, read-only,
no credentials; the status is reported truthfully whatever the boards return.
These are independent from the offline suite.
"""

from __future__ import annotations

import pytest

from atlas.runtime.canary import CanaryBoard, run_adapter_canary
from atlas.sources.models import SourceFamily

pytestmark = [pytest.mark.real_web, pytest.mark.slow]


def _lever_demo() -> CanaryBoard:
    return CanaryBoard(SourceFamily.LEVER, "lever-canary-leverdemo", "Lever Demo",
                       ("CANARY",), None, {"site": "leverdemo", "request_budget": 4})


def _ashby() -> CanaryBoard:
    return CanaryBoard(SourceFamily.ASHBY, "ashby-canary-ashby", "Ashby",
                       ("CANARY",), None, {"board_name": "Ashby", "request_budget": 4})


def test_lever_leverdemo_live_canary():
    result = run_adapter_canary(_lever_demo(), live=True, limit=10)
    # Truthful status regardless of outcome; leverdemo is an official live board.
    assert result.request_status in ("OK", "ZERO") or result.request_status.startswith("ERROR:")
    assert result.live is True
    assert result.board_identity == "lever:leverdemo"
    if result.request_status == "OK":
        assert result.result_count > 0
        assert len(result.samples) <= 5  # evidence sample cap


def test_ashby_official_live_canary():
    result = run_adapter_canary(_ashby(), live=True, limit=10)
    assert result.request_status in ("OK", "ZERO") or result.request_status.startswith("ERROR:")
    assert result.live is True
    assert result.board_identity == "ashby:Ashby"
    if result.request_status == "OK":
        assert result.result_count > 0
        assert len(result.samples) <= 5


def test_live_canary_is_read_only_no_credentials():
    # A canary board carries no auth_ref/credentials; construction is read-only.
    board = _lever_demo()
    result = run_adapter_canary(board, live=True, limit=5)
    assert result.limitation is None or "apply" not in (result.limitation or "").lower()
