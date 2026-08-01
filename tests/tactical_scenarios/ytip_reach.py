"""Y-tip dead-end — reach the tip before reversing out.

Real t360-377 of the live voyage ai_nav_2026-07-28T15-45-44 (Cairo →
Y-tip → back).  The ship sails SE into the Y-tip dead-end pocket,
holding toward the tip (anchor_dist shrinking).  The exit is roughly
opposite the approach, so migrating to it is a ~126° REVERSAL.

Regression: a validated exit-migrate would fire the moment it became
persistent (t377, streak=7 persist=3) and reverse the ship while it was
still ~87 px short of the tip — cutting the corner ~2× the 40 px reach
distance.  The dead-end reach gate defers a >90° reversal migrate until
the ship has actually reached the tip (within TURNING_APPROACH_PX=40),
so it fully enters the pocket first.

A moderate-angle migrate (a real bend/fork, e.g. `nubia_bend` bot→right
~40°) is NOT deferred — that's guarded separately.

Expected: no >60° single-tick commit swing over t360-377 (the ship
holds SE into the pocket).  Pre-fix the migrate reverses commit
152°→279° at t377 (Δ127°).
"""
from .scenario import Scenario

SCENARIO = Scenario(
    name="ytip_reach",
    session="ai_nav_2026-07-28T15-45-44",
    start_tick=360,
    end_tick=377,
    initial_commit_deg=180.0,
    hug_side="port",
    max_commit_rotations=0,        # no >60° reversal before the tip is reached
    commit_rotation_thr_deg=60.0,
    note="Y-tip dead-end; defer the reversal-migrate until the ship reaches "
         "the tip (40px), instead of turning ~87px short",
)
