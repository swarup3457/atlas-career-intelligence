"""Atlas production planning (Phase 1B).

Task archetypes and the coverage planner that turns due companies + policy
into a sealed, fingerprinted coverage plan without a blind Cartesian
explosion. See build spec section 15.
"""

from atlas.planning.planner import (
    CoveragePlanner,
    PlanInput,
    PlannedCompany,
    TaskArchetype,
    cartesian_upper_bound,
)

__all__ = [
    "TaskArchetype",
    "PlannedCompany",
    "PlanInput",
    "CoveragePlanner",
    "cartesian_upper_bound",
]
