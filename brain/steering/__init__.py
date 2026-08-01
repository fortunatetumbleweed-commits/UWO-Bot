"""Steering layer — the §13.17 refactor seam.

See docs/steering_architecture.md for the design.  Phase 1 of the
migration introduces the `CollisionAvoider` Protocol and its first
implementation (`VFHPlusAvoider`); higher-level layers (Goal,
WaypointGenerator, ConfigSelector, Mission) follow in subsequent
phases.
"""
from brain.steering.avoider import (
    AvoiderResult,
    CollisionAvoider,
    VFHPlusAvoider,
    VFHPlusConfig,
)
from brain.steering.config_selector import (
    ConfigSelector,
    NARROW_SAFETY_DIST,
)

__all__ = (
    "AvoiderResult",
    "CollisionAvoider",
    "ConfigSelector",
    "NARROW_SAFETY_DIST",
    "VFHPlusAvoider",
    "VFHPlusConfig",
)
