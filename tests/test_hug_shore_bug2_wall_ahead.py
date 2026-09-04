"""§13.16 — Tests for the Bug2 / Trémaux wall-ahead commit.

When ahead has heavy shore (sec0.frac >= AHEAD_WALL_FRAC), the bot
must deterministically commit to one turn direction (sec 7 for
starboard, sec 1 for port) and STAY committed until ahead clears
(sec0.frac < WALL_AHEAD_EXIT_FRAC).  This prevents the per-tick
flip-flop oscillation documented in hug_debug_20260531_150415 t=46-50
where the bot alternated sec 1 / sec 7 every tick at Port Said and
ended up stuck.
"""
from __future__ import annotations

from brain.goals.hug_shore import (
    HugShoreGoal,
    HugPhase,
    AHEAD_WALL_FRAC,
    WALL_AHEAD_EXIT_FRAC,
)
from brain import observation as _obs
import pytest


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
    __slots__ = ("sectors", "ship_heading_deg", "speed_kt")
    def __init__(self):
        self.speed_kt = 11.0


def _build_nav(sector_specs, heading=0.0):
    # §13.20: auto-expand legacy 8-spec format to 16.
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


def _new_goal(side="starboard"):
    return HugShoreGoal(side=side, max_ticks=20, driver_mode="vfh")


@pytest.mark.simulation  # simulated goal-loop run: `tick()` executes the real action code against whatever frame the fixtures supply
def test_starboard_commit_to_sec7_when_wall_ahead():
    """Heavy shore directly ahead → commit to sec 7 (left turn)."""
    nav = _build_nav([
        (0.65, 0.12),   # sec0 — walled
        (0.30, 0.20),
        (0.20, 0.30),
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.25, 0.25),
    ])
    _obs.update(nav=nav, frame_id="bug2-stbd-commit")
    goal = _new_goal("starboard")
    result = goal.tick()
    # After Bug2 commit, goal's chosen sector should be 7.
    assert goal._wall_ahead_commit == 7, (
        f"Expected commit to sec 7 (Trémaux left for starboard); "
        f"got {goal._wall_ahead_commit}"
    )
    # And the action string should reflect a left turn.
    assert "left" in result.action or result.action == "wait", (
        f"Expected left-turn action after Bug2 commit; got {result.action!r}"
    )


@pytest.mark.simulation  # simulated goal-loop run: `tick()` executes the real action code against whatever frame the fixtures supply
def test_port_commit_to_sec1_when_wall_ahead():
    """Mirror: port hugger commits to sec 1 (right turn) for wall-ahead."""
    nav = _build_nav([
        (0.65, 0.12),
        (0.25, 0.25),
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.20, 0.30),
        (0.30, 0.20),
    ])
    _obs.update(nav=nav, frame_id="bug2-port-commit")
    goal = _new_goal("port")
    result = goal.tick()
    assert goal._wall_ahead_commit == 1
    assert "right" in result.action or result.action == "wait"


@pytest.mark.simulation  # simulated goal-loop run: `tick()` executes the real action code against whatever frame the fixtures supply
def test_commit_persists_until_ahead_clears():
    """The discipline is 'commit and stay' — once committed, the bot
    holds its direction through multiple ticks while ahead remains
    walled (frac >= AHEAD_WALL_FRAC).  This is the anti-flip
    guarantee."""
    nav_walled = _build_nav([
        (0.65, 0.12),
        (0.30, 0.20),
        (0.20, 0.30),
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.25, 0.25),
    ])
    goal = _new_goal("starboard")
    _obs.update(nav=nav_walled, frame_id="bug2-persist-1")
    goal.tick()
    assert goal._wall_ahead_commit == 7

    # Second tick, ahead still walled — commit should persist.
    _obs.update(nav=nav_walled, frame_id="bug2-persist-2")
    goal.tick()
    assert goal._wall_ahead_commit == 7

    # Third tick, ahead still walled — commit persists.
    _obs.update(nav=nav_walled, frame_id="bug2-persist-3")
    goal.tick()
    assert goal._wall_ahead_commit == 7


@pytest.mark.simulation  # simulated goal-loop run: `tick()` executes the real action code against whatever frame the fixtures supply
def test_commit_releases_when_ahead_clears():
    """When the bot escapes (ahead.frac drops below WALL_AHEAD_EXIT_FRAC),
    the commit is released — VFH+ takes over for normal hugging."""
    nav_walled = _build_nav([(0.65, 0.12)] + [(0.0, None)] * 7)
    nav_clear  = _build_nav([(0.05, 0.50)] + [(0.0, None)] * 7)

    goal = _new_goal("starboard")
    _obs.update(nav=nav_walled, frame_id="bug2-rel-1")
    goal.tick()
    assert goal._wall_ahead_commit == 7

    _obs.update(nav=nav_clear, frame_id="bug2-rel-2")
    goal.tick()
    assert goal._wall_ahead_commit is None, (
        f"Expected commit released when ahead clears; "
        f"goal._wall_ahead_commit = {goal._wall_ahead_commit}"
    )


@pytest.mark.simulation  # simulated goal-loop run: `tick()` executes the real action code against whatever frame the fixtures supply
def test_no_commit_below_entry_threshold():
    """Ahead with modest shore (frac < AHEAD_WALL_FRAC) — Bug rule
    doesn't fire; VFH+ decides normally."""
    nav = _build_nav([
        (0.20, 0.30),   # below 0.30 threshold
        (0.40, 0.30),
        (0.60, 0.20),
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
    ])
    _obs.update(nav=nav, frame_id="bug2-no-commit")
    goal = _new_goal("starboard")
    goal.tick()
    assert goal._wall_ahead_commit is None


@pytest.mark.simulation  # simulated goal-loop run: `tick()` executes the real action code against whatever frame the fixtures supply
def test_hysteresis_between_entry_and_exit():
    """ahead.frac in (WALL_AHEAD_EXIT_FRAC, AHEAD_WALL_FRAC) preserves
    the committed state if entered, doesn't enter if not yet committed.
    This is the hysteresis loop that prevents per-tick chatter."""
    # 0.20 is between 0.15 (exit) and 0.30 (entry).
    nav_between = _build_nav([(0.20, 0.30)] + [(0.0, None)] * 7)
    nav_walled  = _build_nav([(0.65, 0.12)] + [(0.0, None)] * 7)

    # Case 1: starting fresh — between thresholds doesn't commit.
    goal1 = _new_goal("starboard")
    _obs.update(nav=nav_between, frame_id="bug2-hyst-1a")
    goal1.tick()
    assert goal1._wall_ahead_commit is None

    # Case 2: already committed — between thresholds preserves commit.
    goal2 = _new_goal("starboard")
    _obs.update(nav=nav_walled, frame_id="bug2-hyst-2a")
    goal2.tick()
    assert goal2._wall_ahead_commit == 7
    _obs.update(nav=nav_between, frame_id="bug2-hyst-2b")
    goal2.tick()
    assert goal2._wall_ahead_commit == 7  # still committed
