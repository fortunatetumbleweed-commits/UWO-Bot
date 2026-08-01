"""Y-tip dead-end pocket — anchor must not leap while ship traverses it.

Real t348-358 of session ai_nav_2026-07-28T00-27-25.  The ship is
descending SE toward a dead-end pocket at the Y-tip; the tactical anchor
sits on the pocket tip (bot edge).  At t356 the tip DETACHES from the
frame edge (becomes interior water), so `_find_run_midpoint_on_edge`
finds no bot-edge run near the shifted prev-WP and sticky fails.  The
trace now ends at the far bottom-left corner (still classified "bot"),
and the within-edge `new_anchor` branch — which, unlike the cross-edge
`migrate` path, has no route-plausibility guard — adopts that far
endpoint.  The anchor leaps ~260px along the bot edge (tip → far corner)
and commit flips +101° (143°→244°) in one tick while the ship still has
to sail into the pocket and turn around.

Expected: the anchor holds near the dead-end tip (dead-reckoned) while
the ship still has to traverse the pocket, so commit rotates smoothly
(no >60° single-tick swing).  Runs as a live-run-style perception replay
(ensemble heading, perceived mask/motion, dead-reckoned position).
"""
from .scenario import Scenario

SCENARIO = Scenario(
    name="ytip_pocket_hold",
    session="ai_nav_2026-07-28T00-27-25",
    start_tick=348,
    end_tick=358,
    initial_commit_deg=180.0,
    hug_side="port",
    max_commit_rotations=0,        # no >60° single-tick commit swing
    commit_rotation_thr_deg=60.0,
    note="Y-tip dead-end pocket; anchor must not leap tip→far-corner mid-traverse",
)
