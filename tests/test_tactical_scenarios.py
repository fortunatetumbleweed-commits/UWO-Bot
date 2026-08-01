"""Pytest wrapper for tactical layer scenarios.

Each scenario runs against real session images with an override on
initial commit direction.  Assertions check WP flip count, bootstrap
count, dominant edge, and bearing range.

Skips scenarios whose session dir is missing (e.g., on CI where
session data isn't checked in).
"""
from pathlib import Path
import pytest

from tests.tactical_scenarios.scenario import run_scenario, assess
from tests.tactical_scenarios import (
    cairo_start, cairo_start_reflex, nubia_bend, y_fork, y_tip_return, lake,
    ytip_pocket_hold, ytip_reach, blue_band, open_water, nile_exit_left,
    med_hug_west, nile_meander, strait_t1525, channel_stuck_t1444,
)

SCENARIOS = [
    cairo_start.SCENARIO,
    cairo_start_reflex.SCENARIO,
    nubia_bend.SCENARIO,
    y_fork.SCENARIO,
    y_tip_return.SCENARIO,
    lake.SCENARIO,
    ytip_pocket_hold.SCENARIO,
    ytip_reach.SCENARIO,
    blue_band.SCENARIO,
    open_water.SCENARIO,
    nile_exit_left.SCENARIO,
    med_hug_west.SCENARIO,
    nile_meander.SCENARIO,
    strait_t1525.SCENARIO,
    channel_stuck_t1444.SCENARIO,
]

SESSIONS_ROOT = Path(__file__).resolve().parents[1] / "data" / "sessions"


@pytest.mark.parametrize("sc", SCENARIOS, ids=[sc.name for sc in SCENARIOS])
def test_tactical_scenario(sc):
    session_dir = SESSIONS_ROOT / sc.session
    if not session_dir.exists():
        pytest.skip(f"session dir missing: {session_dir}")
    outcomes = run_scenario(sc, SESSIONS_ROOT)
    ok, failures = assess(sc, outcomes)
    if sc.xfail:
        # Known-failing redesign target.  Flag if it starts passing so we
        # can drop the marker.
        if ok:
            pytest.fail(
                f"XPASS: scenario '{sc.name}' now passes — the redesign "
                f"fixed it; remove xfail from the scenario."
            )
        pytest.xfail(sc.xfail_reason or f"{sc.name}: known failure (redesign target)")
    if not ok:
        pytest.fail(
            f"Scenario '{sc.name}' failed:\n" +
            "\n".join(f"  - {f}" for f in failures)
        )
