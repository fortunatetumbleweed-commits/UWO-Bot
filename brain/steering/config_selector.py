"""§13.18 phase 4 — Situational AvoiderConfig selection.

Picks one of two `VFHPlusConfig`s per tick: an open-water config
(default) and a narrow-passage config with a reduced `safety_dist`.
Switches via Schmitt-trigger hysteresis on a sector-density detector
+ a minimum dwell time so the regime can't flap at the boundary.

Rationale (from the 2026-06-01 trace, hug_debug_20260601_114709
t360-378):  in a winding tight river, 4-5 of 8 sectors have
`frac > 0.8` simultaneously.  With the open-water `safety_dist=0.12`,
sec 0 (ahead) is masked any time the bend wall is within 0.12 units.
Picker is then forced into ±45° turns every tick, accumulating into
a spiral that bounces the bot back and forth between bends.  Lowering
`safety_dist` to 0.06 in narrow passages unmasks sec 0, lets the bot
creep forward, and the μ2 inertia term holds it on course.

Hysteresis numbers from Quinlan-Khatib 2003 / Marder-Eppstein 2010:
- 20% threshold gap on the detector (enter at ≥5, exit at ≤3)
- 1 s min dwell ≈ 2 ticks at our ~2 s cadence

See docs/steering_architecture.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from brain.steering.avoider import VFHPlusConfig


# Detector thresholds — dense sector = land_fraction > DENSE_FRAC.
DENSE_FRAC = 0.8
# Schmitt trigger thresholds.
ENTER_DENSE_COUNT = 5      # ≥ this many dense → enter "narrow"
EXIT_DENSE_COUNT = 3       # ≤ this many dense → exit "narrow"
# Min ticks the regime must be held before another switch.
MIN_DWELL_TICKS = 2

# Default narrow-config safety distance.  Half of open-water 0.12.
NARROW_SAFETY_DIST = 0.06


@dataclass(frozen=True)
class SelectorDiagnostics:
    """Per-tick selector telemetry for the trace.  All fields are
    optional / nullable so the consumer can dict-ify without worrying
    about absence."""
    regime: str                    # "open" | "narrow"
    dense_count: int
    ticks_in_regime: int
    config_safety_dist: float


def count_dense_sectors(nav, dense_frac: float = DENSE_FRAC) -> int:
    """Count observed sectors with land_fraction strictly above
    `dense_frac` — the "wall on that side" signal."""
    if nav is None or not getattr(nav, "sectors", None):
        return 0
    n = 0
    for s in nav.sectors:
        if getattr(s, "is_observed", False) and s.land_fraction > dense_frac:
            n += 1
    return n


class ConfigSelector:
    """Stateful selector — keeps track of the current regime and how
    long it's been there so the Schmitt + dwell hysteresis can fire.

    Usage:
        selector = ConfigSelector(open_cfg=..., narrow_cfg=...)
        cfg = selector.pick(nav)   # per tick, before calling avoider
        diag = selector.diagnostics()   # for the trace
    """
    def __init__(
        self,
        open_cfg: VFHPlusConfig,
        narrow_cfg: Optional[VFHPlusConfig] = None,
        *,
        enter_dense_count: int = ENTER_DENSE_COUNT,
        exit_dense_count: int = EXIT_DENSE_COUNT,
        min_dwell_ticks: int = MIN_DWELL_TICKS,
        dense_frac: float = DENSE_FRAC,
    ):
        if narrow_cfg is None:
            narrow_cfg = open_cfg   # single-config mode (no switching)
        self.open_cfg = open_cfg
        self.narrow_cfg = narrow_cfg
        self.enter_dense_count = enter_dense_count
        self.exit_dense_count = exit_dense_count
        self.min_dwell_ticks = min_dwell_ticks
        self.dense_frac = dense_frac
        self._regime: str = "open"          # "open" | "narrow"
        self._ticks_in_regime: int = 0
        self._last_dense_count: int = 0

    @property
    def regime(self) -> str:
        return self._regime

    def pick(self, nav) -> VFHPlusConfig:
        """Update the regime via Schmitt + dwell, return the matching
        config.  Tick this once per perception tick before calling the
        avoider."""
        n_dense = count_dense_sectors(nav, dense_frac=self.dense_frac)
        self._last_dense_count = n_dense
        self._ticks_in_regime += 1

        if self._regime == "open":
            if (n_dense >= self.enter_dense_count
                    and self._ticks_in_regime >= self.min_dwell_ticks):
                self._regime = "narrow"
                self._ticks_in_regime = 1
        else:   # narrow
            if (n_dense <= self.exit_dense_count
                    and self._ticks_in_regime >= self.min_dwell_ticks):
                self._regime = "open"
                self._ticks_in_regime = 1

        return self.narrow_cfg if self._regime == "narrow" else self.open_cfg

    def diagnostics(self) -> dict:
        """Per-tick diagnostics dict — JSON-safe, intended for the
        trace `pp_avoider_diag` payload."""
        cfg = self.narrow_cfg if self._regime == "narrow" else self.open_cfg
        return {
            "regime": self._regime,
            "dense_count": self._last_dense_count,
            "ticks_in_regime": self._ticks_in_regime,
            "config_safety_dist": cfg.safety_dist,
        }
