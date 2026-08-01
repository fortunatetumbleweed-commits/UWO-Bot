"""Cairo wide-water departure scenario.

Ship exits Cairo harbor into wide water.  Real t1-t29 of session
ai_nav_2026-07-27T15-53-21.  Initial state: sp=0, facing west
(hdg~265°), commit seeded to south (180°) via --commit-bearing.

Tricky properties:
  - Water is wide → port bank can be far from ship
  - Ship at rest initially → shift_motion is unreliable for first ticks
  - Ship rotates from facing-west to going-south over first ~5 ticks
  - The trace start point can drift far or land off-frame

Expected behavior (post fresh-per-tick fix):
  - No catastrophic 90° flips
  - WP settles on bot edge (south exit)
  - Fresh per-tick pick handles wide water without accumulated drift
"""
from .scenario import Scenario

SCENARIO = Scenario(
    name="cairo_start",
    session="ai_nav_2026-07-27T15-53-21",
    start_tick=1,
    end_tick=29,
    initial_commit_deg=180.0,     # south — matches --commit-bearing 180
    hug_side="port",
    max_flips_90deg=0,             # zero 180°-class flips expected
    max_bootstrap_count=4,         # a few new_anchor during initial settling ok
    expected_dominant_edge="bot",  # majority of ticks should be on bot exit
    expected_wp_bearing_range=(120.0, 240.0),  # commit stays roughly south (150-210 typical)
    note="Cairo wide-water departure; tests handling of far/off-frame starts",
)
