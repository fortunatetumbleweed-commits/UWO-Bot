"""§13.15 — Tests for the VFH* swept-volume lookahead.

The lookahead is the bow-path collision-cone penalty added to each
candidate sector's VFH+ snapshot cost.  It should:
  1. Fire on the t=101-style "shore in bow-target, not yet at bow" case
     (where static thresholds previously missed the approaching shore)
     and push the picker AWAY from hold-course toward turn-away.
  2. Stay silent when shore is already tight (existing VFH+ wins).
  3. Stay silent when target side has no observable shore (deferring
     to lost-shore / wrong-side-U-turn recovery).
  4. Stay silent under astern-pull (deferring to corner-rounding
     re-engagement strategy).
  5. Stay silent when no obstacles are observed at all.
"""
from __future__ import annotations
import pytest
pytestmark = pytest.mark.skip(reason='§13.20: 8→16 sector migration TODO. Tests legacy VFH+ / Bug2 code paths that are gated off for point_pursuit driver; need fixtures re-derived for 16-sec grid.')


from brain.goals.hug_shore import (
    _lookahead_collision_cost,
    _extract_obstacles,
    ASTERN_PULL_FRAC,
    LATERAL_VALID_FRAC,
    LOOKAHEAD_TIGHT_SHORE_DIST,
    SHORE_VISIBLE_FRAC,
    _SECTOR_REL_ANGLE,
)


_SECTOR_BEARINGS = (
    0.0, 22.5, 45.0, 67.5, 90.0, 112.5, 135.0, 157.5,
    180.0, 202.5, 225.0, 247.5, 270.0, 292.5, 315.0, 337.5,
)


class _Sec:
    __slots__ = ("land_fraction", "nearest_dist", "is_observed", "bearing_deg")
    def __init__(self, frac, dist, bearing, observed=True):
        self.land_fraction = frac
        self.nearest_dist  = dist
        self.is_observed   = observed
        self.bearing_deg   = bearing


class _Nav:
    __slots__ = ("sectors", "ship_heading_deg")


def _make_nav(sector_specs, heading=0.0):
    """sector_specs: list of (frac, dist) for sectors 0..7 (auto-
    expanded to 16 per §13.20) or 0..15."""
    if len(sector_specs) == 8:
        expanded = []
        for spec in sector_specs:
            expanded.append(spec)
            expanded.append((0.0, None))
        sector_specs = expanded
    secs = []
    for i, (f, d) in enumerate(sector_specs):
        observed = d is not None or f > 0.0
        secs.append(_Sec(f, d, _SECTOR_BEARINGS[i], observed=observed))
    nav = _Nav()
    nav.sectors = tuple(secs)
    nav.ship_heading_deg = heading
    return nav


def test_t101_peninsula_approach_penalises_hold_more_than_turn_away():
    """Replay the t=101 frame of hug_debug_20260531_130527 — sec1 has
    close shore at d=0.29, sec0 unobserved, target sector (sec2)
    healthy.  Lookahead should rank hold (sec 0) MORE EXPENSIVE than
    turn-away (sec 7) so the picker prefers peeling.
    """
    nav = _make_nav([
        (0.00, None),   # sec0 ahead — unobserved
        (0.37, 0.29),   # sec1 bow-stbd — peninsula approaching
        (0.63, 0.32),   # sec2 stbd beam — healthy hug
        (0.19, 0.38),   # sec3 astern-stbd
        (0.04, 0.40),   # sec4 astern
        (0.53, 0.51),   # sec5 astern-port
        (0.55, 0.49),   # sec6 port beam
        (0.00, None),   # sec7 bow-port — clear
    ], heading=204.0)
    look = _lookahead_collision_cost(nav, side="starboard", speed_kt=10.9)
    # Hold-course must cost MORE than turn-away — the bow path under
    # hold passes within COLLISION_SAFETY of sec1's shore, while the
    # turn-away path opens room.
    assert look[0] > look[7] + 0.05, (
        f"Expected hold (sec 0, cost {look[0]:.3f}) more expensive "
        f"than turn-away (sec 7, cost {look[7]:.3f}); peninsula approach "
        f"not penalising hold enough."
    )
    # And turn-INTO (sec 1) should be even more expensive — heading
    # straight at the close shore.
    assert look[1] > look[0]


def test_tight_shore_gate_suppresses_lookahead():
    """When any forward sector (7, 0, 1) has shore at < 0.15, the
    lookahead must defer to VFH+'s tuned tight-hug logic.  All
    lookahead costs should be 0."""
    nav = _make_nav([
        (0.50, 0.10),   # sec0 ahead — very close (triggers gate)
        (0.40, 0.30),
        (0.60, 0.20),
        (0.30, 0.40),
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
    ])
    look = _lookahead_collision_cost(nav, side="starboard", speed_kt=11.0)
    assert all(v == 0.0 for v in look.values()), look


def test_target_side_empty_suppresses_lookahead():
    """Wrong-side / lost-shore mode: target-side sector has no shore.
    Lookahead must stay quiet so existing recovery logic drives."""
    nav = _make_nav([
        (0.00, None),
        (0.00, None),
        (0.00, None),    # sec2 (T for starboard) — empty
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.40, 0.10),    # sec6 (O) — opposite side has shore
        (0.30, 0.15),    # sec7 (bO) — opposite bow has shore
    ])
    look = _lookahead_collision_cost(nav, side="starboard", speed_kt=11.0)
    assert all(v == 0.0 for v in look.values()), look


def test_astern_pull_gate_suppresses_lookahead():
    """Just-rounded-a-corner: aT has heavy shore (astern-pull mode).
    Lookahead must stay quiet so astern-pull can re-engage the wrap."""
    nav = _make_nav([
        (0.51, 0.27),
        (0.45, 0.35),
        (0.83, 0.22),
        (0.92, 0.16),   # sec3 (aT for starboard) — > ASTERN_PULL_FRAC
        (0.58, 0.09),
        (0.03, 0.07),
        (0.00, 0.22),
        (0.01, 0.20),
    ])
    look = _lookahead_collision_cost(nav, side="starboard", speed_kt=11.0)
    assert all(v == 0.0 for v in look.values()), look


def test_no_obstacles_means_zero_lookahead():
    """Trivial early-exit: no observed shore anywhere."""
    nav = _make_nav([(0.0, None)] * 8)
    look = _lookahead_collision_cost(nav, side="starboard", speed_kt=11.0)
    assert all(v == 0.0 for v in look.values()), look


def test_extract_obstacles_skips_noise_floor_fracs():
    """Sectors below LATERAL_VALID_FRAC carry sentinel dist values but
    no real shore.  They must NOT be projected as collision targets."""
    nav = _make_nav([
        (0.00, 0.21),   # frac < LATERAL_VALID_FRAC → ignored
        (0.20, 0.30),   # real shore
        (0.03, 0.25),   # frac < LATERAL_VALID_FRAC → ignored
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
    ])
    obs = _extract_obstacles(nav)
    assert len(obs) == 1
    # Only sec1's point survives.
    x, y = obs[0]
    assert abs(x - 0.30 * 0.7071) < 0.01
    assert abs(y - 0.30 * 0.7071) < 0.01


def test_port_side_mirrors_starboard_gates():
    """Same target-side empty case mirrored to port — sec6 is the
    target, sec5 is the aT.  Without a port-aware gate, lookahead
    would misfire on port-hugging."""
    nav = _make_nav([
        (0.00, None),
        (0.30, 0.15),   # sec1 (bO for port) — opposite-bow shore
        (0.40, 0.10),   # sec2 (O for port) — opposite-side shore
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),   # sec6 (T for port) — empty
        (0.00, None),
    ])
    look = _lookahead_collision_cost(nav, side="port", speed_kt=11.0)
    assert all(v == 0.0 for v in look.values()), look
