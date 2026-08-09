"""Structured PerceivedState — the A2 consolidation target.

One arbitrated perception truth per tick, each field read by its BEST signal:

    base:      coarse STRUCTURE     overworld | world_map | loading | panel
    overlay:   ORTHOGONAL popup     none | dialog | confirm | negotiation |
                                     result | announcement | main_menu
    mode:      sea | port           (only meaningful when base == overworld)
    context:   which panel          Market | Inn | Village | …  (base == panel)
    menu_item: active left-menu item Buy | Sell | Hire | …  (the tiered-label
                                     FUNCTION field — screen_tags.json ground truth)
    identity:  universal top-left slot read (icon+name / title)
    conf:      per-field confidence on one 0..1 scale

Phase 0 (this file): the TYPE plus a `legacy_state()` mapping back to the old
where_am_i() `state` vocabulary — so the 40+ consumers of `PerceiveResult.state`
keep working while new code reads the structured fields — and `from_legacy()`
to derive a structured view from today's flat classifier output for measurement.
Later phases compute the fields independently and collapse the 22-return cascade.
See docs/a2_perceived_state_implementation_plan.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ── base (coarse structure) ───────────────────────────────────────────────────
BASE_OVERWORLD = "overworld"
BASE_WORLD_MAP = "world_map"
BASE_LOADING   = "loading"
BASE_PANEL     = "panel"
BASE_UNKNOWN   = "unknown"

# ── overlay (orthogonal to base) ──────────────────────────────────────────────
OVERLAY_NONE      = "none"
OVERLAY_DIALOG    = "dialog"
OVERLAY_MAIN_MENU = "main_menu"

# ── mode (only when base == overworld) ────────────────────────────────────────
MODE_SEA  = "sea"
MODE_PORT = "port"


@dataclass
class PerceivedState:
    base:      str = BASE_UNKNOWN
    overlay:   str = OVERLAY_NONE
    mode:      Optional[str] = None
    context:   Optional[str] = None
    menu_item: Optional[str] = None
    identity:  Optional[str] = None
    conf:      dict = field(default_factory=dict)
    # Round-trip fidelity for Phase 0: the exact legacy `location` string this
    # was derived from, used as the fall-through in legacy_state() for states
    # the structured mapping doesn't model yet (learned fingerprints, `pending`).
    # Dropped once the fields are computed directly (Phase 4+).
    legacy_raw: Optional[str] = None

    # ── mapping back to the old where_am_i() vocabulary ───────────────────────
    def legacy_state(self) -> str:
        """The old `state` string, derived from the structured fields.

        Kept an exact inverse of `from_legacy` so authority can flip later
        without breaking the 40+ consumers that read `PerceiveResult.state`.
        """
        if self.overlay == OVERLAY_MAIN_MENU:
            return "main_menu"
        if self.base == BASE_OVERWORLD:
            if self.mode == MODE_SEA:
                return "sea"
            if self.mode == MODE_PORT:
                return "port_overworld"
            return self.legacy_raw or "port_overworld"
        if self.base == BASE_WORLD_MAP:
            return "world_map"
        if self.base == BASE_LOADING:
            # distinguishes loading vs pending, both of which fold to loading
            return self.legacy_raw or "loading"
        if self.base == BASE_PANEL:
            if (self.context or "").strip().lower() == "village":
                return "village"
            # building / sub_menu / learned-fingerprint states preserved raw
            return self.legacy_raw or "building"
        return self.legacy_raw or "unknown"

    # ── deriving the structured view from today's flat classifier output ──────
    @classmethod
    def from_legacy(
        cls,
        location: str,
        port: Optional[str] = None,
        detail: str = "",
        has_overlay: bool = False,
    ) -> "PerceivedState":
        """Build a PerceivedState from the legacy {location, port, detail}.

        Phase-0 plumbing: lossy but round-trip-faithful (from_legacy(x)
        .legacy_state() == x for every legacy state). `has_overlay` seeds the
        orthogonal overlay axis coarsely from "an interruptor is still up".
        """
        loc = (location or "").strip()
        overlay = OVERLAY_DIALOG if has_overlay else OVERLAY_NONE

        if loc == "main_menu":
            return cls(base=BASE_OVERWORLD, overlay=OVERLAY_MAIN_MENU,
                       legacy_raw=loc)
        if loc == "sea":
            return cls(base=BASE_OVERWORLD, mode=MODE_SEA, overlay=overlay,
                       identity=port, legacy_raw=loc)
        if loc == "port_overworld":
            return cls(base=BASE_OVERWORLD, mode=MODE_PORT, overlay=overlay,
                       identity=port, context=port, legacy_raw=loc)
        if loc == "world_map":
            return cls(base=BASE_WORLD_MAP, overlay=overlay, legacy_raw=loc)
        if loc in ("loading", "pending"):
            return cls(base=BASE_LOADING, overlay=overlay, legacy_raw=loc)
        if loc == "village":
            return cls(base=BASE_PANEL, context="Village", overlay=overlay,
                       identity=port, legacy_raw=loc)
        if loc == "unknown" or not loc:
            return cls(base=BASE_UNKNOWN, overlay=overlay, legacy_raw=loc or "unknown")
        # "building" and any learned-fingerprint state → a panel; keep the raw
        # id as context so legacy_state() reproduces it exactly.
        return cls(base=BASE_PANEL, context=port, overlay=overlay,
                   identity=port, legacy_raw=loc)


# Legacy `location` value → coarse base, for scoring/labeling helpers.
LEGACY_TO_BASE = {
    "sea":            BASE_OVERWORLD,
    "port_overworld": BASE_OVERWORLD,
    "world_map":      BASE_WORLD_MAP,
    "loading":        BASE_LOADING,
    "pending":        BASE_LOADING,
    "village":        BASE_PANEL,
    "building":       BASE_PANEL,
    "main_menu":      BASE_OVERWORLD,   # main_menu is an overlay over overworld
    "unknown":        BASE_UNKNOWN,
}
