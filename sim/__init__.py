"""Hug-shore navigation simulator.

A leaf-dependency package: imports only from `vision.navigation_view`
(the data contract) and `brain.goals.hug_shore` (system under test).
Nothing in the runtime imports from here.

See `docs/hug_shore_scenarios.md` for the test plan and conventions.
"""

from .world import World, Ship, Polygon
from .perception import synthesize_nav_view, VIEW_RADIUS, RAYS_PER_SECTOR
from .dynamics import apply_action, run_sim, TickRecord

__all__ = [
    "World",
    "Ship",
    "Polygon",
    "synthesize_nav_view",
    "VIEW_RADIUS",
    "RAYS_PER_SECTOR",
    "apply_action",
    "run_sim",
    "TickRecord",
]
