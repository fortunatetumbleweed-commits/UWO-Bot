"""Lake exploration and return scenario.

t488-t589 of session ai_nav_2026-07-27T17-47-27.  Ship enters
Lake Victoria area heading SE at lat 2.34, progresses south
through the lake to the southernmost point (lat 0.02 at t563),
then reverses and heads back north.

Tricky properties:
  - Lake has "dead-end" shape — ship goes in, reaches terminal
    area, must turn around and exit
  - Water topology shifts dramatically as ship navigates around
    the lake perimeter
  - WP anchor legitimately transitions bot → top edge at the
    turnaround (t548) as the return path becomes the correct
    exit direction

Expected behavior:
  - Sticky(bot) as ship enters and progresses south
  - Anchor migration or new_anchor at the turnaround
  - Sticky(top) on the return leg
  - Reaches lake southernmost point (lat < 1.0)
  - No 180° flip that would cause the ship to loop back too early
"""
from .scenario import Scenario

SCENARIO = Scenario(
    name="lake",
    session="ai_nav_2026-07-27T17-47-27",
    start_tick=488,
    end_tick=589,
    initial_commit_deg=128.0,     # SE, ship's real commit at t488
    hug_side="port",
    # Pre-seed with t487 WP on bot edge (entering the lake).
    initial_wp_offset=(93, 120),
    initial_wp_edge="bot",
    max_flips_90deg=2,             # bot→top at turnaround + possibly one
                                    # more small transition
    max_bootstrap_count=None,       # lots of new_anchor around lake perimeter
                                    # is expected — the run had 36 total
    expected_dominant_edge=None,    # split bot (inbound) + top (outbound)
    expected_wp_bearing_range=None, # bearing changes drastically at
                                    # turnaround — no single range fits
    note="Lake Victoria dead-end shape; ship enters SE, reaches lat 0.02, "
         "reverses course and exits north; tests turnaround handling",
)
