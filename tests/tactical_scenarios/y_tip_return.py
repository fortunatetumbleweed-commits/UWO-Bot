"""Y-tip return from dead-end — reverse-direction sticky test.

t442-t455 of session ai_nav_2026-07-25T11-01-09.  Ship has reached
the Y-tip dead-end and is now returning back up the river toward
the source.  Ship heading is NW (~315°), commit direction ~310° NW.

Tricky properties:
  - Ship's direction is now REVERSED from the outbound leg
  - Port bank has SWITCHED sides (port of a NW-going ship is SW,
    opposite of what it was for the SE-going outbound leg)
  - The trace start point needs to be on the SW-side bank now,
    not the NE-side bank that was port during outbound

Expected behavior:
  - Sticky-anchor pinned to top-left / left-edge area (the exit
    back out of the Y-tip pocket toward the river source)
  - Fresh-per-tick pick correctly finds the port-side bank for
    the reversed direction (no perpetuation of outbound port
    bank via persistent-start drift — that's why the earlier
    persistent-start design failed here)
  - WP bearing stays in NW quadrant (270°-360°) or west (~280°)
"""
from .scenario import Scenario

SCENARIO = Scenario(
    name="y_tip_return",
    session="ai_nav_2026-07-25T11-01-09",
    start_tick=442,
    end_tick=455,
    initial_commit_deg=310.0,     # NW, ship's real commit at t442
    hug_side="port",
    # Pre-seed: at t441 the WP was (-93, -118) on top edge — that
    # becomes the "prev" for t442's sticky lookup.
    initial_wp_offset=(-93, -118),
    initial_wp_edge="top",
    max_flips_90deg=1,             # top → left transition allowed
    max_bootstrap_count=3,
    expected_dominant_edge=None,    # could be top OR left dominant
    expected_wp_bearing_range=(240.0, 30.0),  # NW/W or slight N — wraps 0°
    note="Y-tip return from dead-end; port bank has switched sides "
         "because ship direction reversed; tests fresh-per-tick pick",
)
