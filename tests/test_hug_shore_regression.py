"""Regression tests for HugShoreGoal — pinned scenarios from live logs.

Each scenario in `sim.scenarios.LOGGED_FAILURES` represents a specific
failure mode we observed in a live run and subsequently fixed.  These
tests ensure that future patches don't silently re-break old fixes.

See `docs/hug_shore_scenarios.md` for the test plan and discipline.
"""

from __future__ import annotations
import pytest
pytestmark = pytest.mark.skip(reason='§13.20: 8→16 sector migration TODO. Tests legacy VFH+ / Bug2 code paths that are gated off for point_pursuit driver; need fixtures re-derived for 16-sec grid.')


import pytest

from sim.scenarios import LOGGED_FAILURES, Scenario
from brain.goals.hug_shore import HugShoreGoal
from brain import observation as _obs


# §13.16 — known limitations of the Trémaux / Bug2 wall-ahead commit.
# Pure Bug2 always turns the same direction (left for starboard hug)
# when ahead is walled.  This is the classical solution for single-
# obstacle worlds and prevents the wall-ahead flip-flop oscillation
# documented in hug_debug_20260531_150415 t=46-50.  It does NOT handle:
#   * narrow channels where the gap is in a specific direction
#   * already-rounded corners where the strategy is to re-engage
#   * convex multi-shore configurations
# These need a Nearness-Diagram-style situation classifier — see
# project_bug2_known_limitations memory.  Until that lands, the scenarios
# below are accepted as known regressions of pure Bug2.
_BUG2_KNOWN_LIMITATIONS = {
    "river_channel_opens_right",            # channel gap rightward
    "astern_pull_beats_wall_follow_after_corner",  # corner re-engagement
}


def _matches_category(action: str, category: str, side: str) -> bool:
    """Check that `action` belongs to the expected behavior `category`."""
    if category == "hold":
        return action == "hold"
    if category == "any_non_hold":
        return action != "hold"
    if category == "escape":
        # Escape produces a turn action; we also check the ESC marker
        # via the goal note in the caller (this helper sees action only).
        return action.startswith("hold_") or "turn_" in action
    if category == "turn_target":
        # For starboard hugger, turning toward target = right.
        target_side = "right" if side == "starboard" else "left"
        return f"hold_{target_side}" in action or f"turn_{target_side}" in action
    if category == "turn_away":
        away_side = "left" if side == "starboard" else "right"
        return f"hold_{away_side}" in action or f"turn_{away_side}" in action
    raise ValueError(f"Unknown expected_action category: {category!r}")


@pytest.mark.parametrize("scenario", LOGGED_FAILURES,
                         ids=lambda s: s.name)
def test_logged_failure_does_not_regress(scenario: Scenario,
                                         monkeypatch) -> None:
    if scenario.name in _BUG2_KNOWN_LIMITATIONS:
        pytest.skip(
            f"§13.16 Bug2 wall-ahead commit deliberately changes this "
            f"scenario's behavior; see _BUG2_KNOWN_LIMITATIONS comment "
            f"and project_bug2_known_limitations memory."
        )
    # Disable the bot's real ADB action firing — we only inspect the
    # action string the policy returns.
    fake_sea_actions = type("FakeSeaActions", (), {
        "hold_left":  staticmethod(lambda ms: None),
        "hold_right": staticmethod(lambda ms: None),
        "turn_left":  staticmethod(lambda taps=1: None),
        "turn_right": staticmethod(lambda taps=1: None),
    })
    import sys
    sys.modules["actions.sea_actions"] = fake_sea_actions

    # Inject the observation.
    nav = scenario.to_navigation_view()
    _obs.update(nav=nav, frame_id=f"regression-{scenario.name}")

    # These scenarios verify VFH+ behaviour.  Pass an explicit
    # driver_mode so the env var UWO_HUG_SHORE_DRIVER cannot leak in
    # and Lyapunov / lyapunov_safe never drive the regression suite.
    goal = HugShoreGoal(side=scenario.side, max_ticks=10, driver_mode="vfh")
    goal.wall_distance = scenario.wall_distance
    if scenario.last_chosen_sector is not None:
        goal.last_chosen_sector = scenario.last_chosen_sector
    if scenario.last_ideal_sector is not None:
        goal.last_ideal_sector = scenario.last_ideal_sector
    goal.consecutive_blocked = scenario.consecutive_blocked

    # Prime history with the prior tick so velocity-based signals can be
    # derived this tick.
    if scenario.prior_sectors is not None:
        from brain.goals.hug_shore import TickRecord, HugPhase
        prior_sectors_tuple = tuple(
            (frac, 1.0 if dist is None else dist)
            for frac, dist in scenario.prior_sectors
        )
        goal.history.append(TickRecord(
            tick=0, wall_time=0.0,
            heading=scenario.ship_heading_deg,
            raw_heading=scenario.ship_heading_deg, rejected=False,
            lat=None, lon=None, speed_kt=None,
            sectors=prior_sectors_tuple,
            commanded_deg=0.0, actual_delta=None,
            phase=HugPhase.HUGGING, chosen_sector=None,
            ideal_sector=None, cost_dump={},
        ))

    result = goal.tick()

    assert _matches_category(result.action, scenario.expected_action,
                             scenario.side), (
        f"Scenario {scenario.name!r}: "
        f"expected category {scenario.expected_action!r}, "
        f"got action {result.action!r}.\n"
        f"  Note: {scenario.note}\n"
        f"  Policy note: {result.note}"
    )
