"""§13.18 phase 4 — Tests for ConfigSelector + the narrow-passage
detector with Schmitt-trigger + dwell hysteresis.

The detector counts observed sectors with land_fraction > 0.8.
Hysteresis: enter "narrow" at ≥5 dense; exit at ≤3 dense; min dwell
2 ticks between regime switches.  See
docs/steering_architecture.md.
"""
from __future__ import annotations

from brain.steering import (
    ConfigSelector,
    NARROW_SAFETY_DIST,
    VFHPlusConfig,
)
from brain.steering.config_selector import count_dense_sectors


_SECTOR_BEARINGS = (
    0.0, 22.5, 45.0, 67.5, 90.0, 112.5, 135.0, 157.5,
    180.0, -157.5, -135.0, -112.5, -90.0, -67.5, -45.0, -22.5,
)


class _Sec:
    __slots__ = ("land_fraction", "nearest_dist", "is_observed", "bearing_deg")
    def __init__(self, f, d, b, observed=True):
        self.land_fraction = f
        self.nearest_dist  = d
        self.is_observed   = observed
        self.bearing_deg   = b


class _Nav:
    __slots__ = ("sectors", "ship_heading_deg")


def _nav(fracs):
    # §13.20: auto-expand legacy 8-frac to 16 by inserting zero-frac
    # in-between sectors at odd indices.  Preserves the original
    # "n dense sectors" semantics.
    if len(fracs) == 8:
        expanded = []
        for f in fracs:
            expanded.append(f)
            expanded.append(0.0)
        fracs = expanded
    secs = tuple(_Sec(f, 0.10, _SECTOR_BEARINGS[i]) for i, f in enumerate(fracs))
    n = _Nav()
    n.sectors = secs
    n.ship_heading_deg = 0.0
    return n


def _open():
    return VFHPlusConfig(safety_dist=0.12)


def _narrow():
    return VFHPlusConfig(safety_dist=NARROW_SAFETY_DIST)


# ── Detector ──


def test_count_dense_sectors_empty_nav_returns_zero():
    assert count_dense_sectors(None) == 0


def test_count_dense_sectors_basic():
    n = _nav([0.9, 0.5, 0.0, 0.85, 0.2, 0.95, 0.81, 0.0])
    # 0.9, 0.85, 0.95, 0.81 = 4 above the 0.8 threshold.
    assert count_dense_sectors(n) == 4


def test_count_dense_sectors_strict_above_threshold():
    n = _nav([0.8] * 8)   # exactly equal, not above
    assert count_dense_sectors(n) == 0


# ── Selector hysteresis ──


def test_selector_starts_in_open_regime():
    s = ConfigSelector(_open(), _narrow())
    assert s.regime == "open"
    cfg = s.pick(_nav([0.0] * 8))
    assert cfg.safety_dist == 0.12
    assert s.regime == "open"


def test_selector_enters_narrow_when_threshold_reached():
    s = ConfigSelector(_open(), _narrow())
    # First call: prime ticks_in_regime so dwell allows a switch.
    s.pick(_nav([0.9] * 5 + [0.0] * 3))  # 5 dense
    cfg = s.pick(_nav([0.9] * 5 + [0.0] * 3))
    assert s.regime == "narrow"
    assert cfg.safety_dist == NARROW_SAFETY_DIST


def test_selector_does_not_enter_narrow_at_4_dense():
    """4 dense sectors is below the enter threshold."""
    s = ConfigSelector(_open(), _narrow())
    s.pick(_nav([0.9] * 4 + [0.0] * 4))
    s.pick(_nav([0.9] * 4 + [0.0] * 4))
    assert s.regime == "open"


def test_selector_exits_narrow_when_clears():
    s = ConfigSelector(_open(), _narrow())
    # Get into narrow.
    s.pick(_nav([0.9] * 5 + [0.0] * 3))
    s.pick(_nav([0.9] * 5 + [0.0] * 3))
    assert s.regime == "narrow"
    # Clear out (≤3 dense) and dwell.
    s.pick(_nav([0.9, 0.0, 0.9, 0.0, 0.9, 0.0, 0.0, 0.0]))   # 3 dense
    s.pick(_nav([0.9, 0.0, 0.9, 0.0, 0.9, 0.0, 0.0, 0.0]))
    assert s.regime == "open"


def test_selector_does_not_exit_narrow_at_4_dense():
    """Hysteresis: 4 dense is between exit threshold (3) and enter
    threshold (5), so neither transition fires.  Stays in narrow."""
    s = ConfigSelector(_open(), _narrow())
    # Get into narrow.
    s.pick(_nav([0.9] * 5 + [0.0] * 3))
    s.pick(_nav([0.9] * 5 + [0.0] * 3))
    assert s.regime == "narrow"
    # Boundary: 4 dense → no switch.
    s.pick(_nav([0.9] * 4 + [0.0] * 4))
    s.pick(_nav([0.9] * 4 + [0.0] * 4))
    assert s.regime == "narrow"


def test_selector_min_dwell_prevents_same_tick_flip():
    """min_dwell_ticks keeps the regime stable across the exact
    moment the detector crosses the boundary."""
    # With min_dwell_ticks=5 and a fresh selector, "open" must persist
    # through several ticks before any "narrow" enter trigger fires —
    # even if dense_count is already at threshold.
    s = ConfigSelector(_open(), _narrow(), min_dwell_ticks=5)
    s.pick(_nav([0.9] * 5 + [0.0] * 3))    # tick 1 — too early to switch
    s.pick(_nav([0.9] * 5 + [0.0] * 3))    # tick 2
    s.pick(_nav([0.9] * 5 + [0.0] * 3))    # tick 3
    s.pick(_nav([0.9] * 5 + [0.0] * 3))    # tick 4
    assert s.regime == "open"
    s.pick(_nav([0.9] * 5 + [0.0] * 3))    # tick 5 — dwell satisfied
    assert s.regime == "narrow"


def test_selector_single_config_mode_no_switching():
    """When narrow_cfg is None, both regimes use the same config."""
    s = ConfigSelector(_open())
    cfg_a = s.pick(_nav([0.0] * 8))
    cfg_b = s.pick(_nav([0.9] * 5 + [0.0] * 3))
    assert cfg_a.safety_dist == 0.12
    assert cfg_b.safety_dist == 0.12


def test_selector_diagnostics_shape():
    s = ConfigSelector(_open(), _narrow())
    s.pick(_nav([0.9] * 5 + [0.0] * 3))
    d = s.diagnostics()
    assert d["regime"] in ("open", "narrow")
    assert d["dense_count"] == 5
    assert d["ticks_in_regime"] >= 1
    assert d["config_safety_dist"] in (0.12, NARROW_SAFETY_DIST)


def test_selector_back_and_forth_does_not_meta_oscillate():
    """The trace-driven scenario: dense count oscillates between 4 and
    5 (right at the boundary).  With hysteresis we stay put once
    we're inside narrow."""
    s = ConfigSelector(_open(), _narrow())
    # Get into narrow.
    s.pick(_nav([0.9] * 5 + [0.0] * 3))
    s.pick(_nav([0.9] * 5 + [0.0] * 3))
    assert s.regime == "narrow"
    # Now oscillate 5 ↔ 4.  Neither crosses the exit (3) threshold.
    for _ in range(10):
        s.pick(_nav([0.9] * 4 + [0.0] * 4))
        s.pick(_nav([0.9] * 5 + [0.0] * 3))
    assert s.regime == "narrow"
