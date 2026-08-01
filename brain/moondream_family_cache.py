"""Moondream family-level verdict cache.

Phase 3.3: Moondream answers "what FAMILY is this screen?" (in_town vs
at_sea) reliably but costs ~5–7 s per call.  Calling it on every
classifier tick would slow the bot 10–20×.  This module caches the
verdict and invalidates it on EVENTS (set sail, world-map open, task
start, arrival) or after a TTL (default 10 min).

Why the family verdict is cacheable across many ticks:
  - The bot's family-level location (port vs sea) changes only via
    discrete actions: depart, arrive.  Inside a port or on the open
    sea, it doesn't transition without an explicit event.
  - TTL guards against silent state drift (e.g. an unmodelled event
    that the bot doesn't yet know to invalidate on).

Usage from `_classify_nav_state` (early arbiter, ambiguous-chrome branch):

    cached = moondream_family_cache.get_family()
    if cached == "in_town":
        return {"location": "port_overworld", ...}
    if cached == "at_sea":
        return {"location": "sea", ...}
    # cache miss / stale → call Moondream now and store
    verdict = ...
    moondream_family_cache.set_family(verdict, source="moondream")

Invalidation callers (explicit, not automatic):

  - actions.self_grow.run_self_grow       — reason="task_start"
  - actions.task_runner.run_sail_to       — reason="task_start"
  - actions.sail_actions._depart_from_harbour — reason="set_sail"
  - actions.sail_actions._open_world_map_from_sea — reason="opened_world_map"
  - Any path that detects sea→port_overworld arrival — reason="arrival"
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Optional

from loguru import logger


FamilyVerdict = Literal["in_town", "at_sea", "unknown"]


def _default_ttl_seconds() -> int:
    raw = os.environ.get("MOONDREAM_FAMILY_TTL_SECONDS")
    if raw and raw.isdigit():
        return max(60, int(raw))
    return 600  # 10 minutes


@dataclass
class _CacheState:
    verdict:        Optional[FamilyVerdict] = None
    set_at:         Optional[datetime] = None
    source:         str = ""
    invalidated:    bool = False
    invalid_reason: str = ""


_state = _CacheState()
_lock = threading.Lock()


def get_family() -> Optional[FamilyVerdict]:
    """Return the cached verdict if fresh; None when stale/invalidated.

    Treats both TTL expiry and explicit invalidation as "stale" — the
    caller falls back to a live Moondream call.
    """
    with _lock:
        if _state.verdict is None or _state.invalidated:
            return None
        if _state.set_at is None:
            return None
        age = datetime.now() - _state.set_at
        if age.total_seconds() > _default_ttl_seconds():
            return None
        return _state.verdict


def set_family(verdict: FamilyVerdict, source: str = "moondream") -> None:
    """Store a new family verdict with timestamp."""
    with _lock:
        _state.verdict        = verdict
        _state.set_at         = datetime.now()
        _state.source         = source
        _state.invalidated    = False
        _state.invalid_reason = ""
    logger.info(
        f"[moondream_family_cache] verdict set: {verdict!r} (source={source})"
    )


def invalidate(reason: str) -> None:
    """Mark the cache stale — next get_family() returns None."""
    with _lock:
        if not _state.verdict:
            # Nothing to invalidate; record the reason anyway for telemetry.
            _state.invalid_reason = reason
            _state.invalidated = True
            return
        prior = _state.verdict
        _state.invalidated    = True
        _state.invalid_reason = reason
    logger.info(
        f"[moondream_family_cache] invalidated (was {prior!r}) — reason: {reason}"
    )


def is_fresh() -> bool:
    """True iff the cache has a verdict AND it's within TTL AND not invalidated."""
    return get_family() is not None


def snapshot() -> dict:
    """Return a copy of the cache state for telemetry / debugging."""
    with _lock:
        return {
            "verdict":        _state.verdict,
            "set_at":         _state.set_at.isoformat() if _state.set_at else None,
            "source":         _state.source,
            "invalidated":    _state.invalidated,
            "invalid_reason": _state.invalid_reason,
            "ttl_seconds":    _default_ttl_seconds(),
            "age_seconds":    (
                (datetime.now() - _state.set_at).total_seconds()
                if _state.set_at else None
            ),
        }


def reset() -> None:
    """Clear all state.  Used by tests."""
    with _lock:
        _state.verdict        = None
        _state.set_at         = None
        _state.source         = ""
        _state.invalidated    = False
        _state.invalid_reason = ""
