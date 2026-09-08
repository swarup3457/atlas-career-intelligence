"""Atlas market discovery + adaptive search (Phase 1D).

The market layer adds READ-ONLY portal discovery (LinkedIn/Naukri), dynamic
company registration, portal->official verification, append-only sealed
campaign/waves, and a deterministic adaptive-coverage governor on top of the ONE
existing LangGraph production governor. No second orchestration framework and no
second governor are introduced — the campaign/wave loop is deterministic Python,
never an LLM.
"""

from __future__ import annotations

from atlas.market.campaign import (
    CampaignBudget,
    CampaignStatus,
    MarketCampaign,
    MarketWave,
    WaveStatus,
    WaveTask,
    WaveTaskKind,
)

__all__ = [
    "CampaignBudget",
    "CampaignStatus",
    "MarketCampaign",
    "MarketWave",
    "WaveStatus",
    "WaveTask",
    "WaveTaskKind",
]
