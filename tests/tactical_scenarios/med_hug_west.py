"""Open-Mediterranean hug-side persistence: coast must stay on port.

t105-150 of session ai_nav_2026-07-30T18-09-20 (trimmed into
`med_hug_west_t114`).  After exiting the Nile and turning west, the ship
is in the open Med (water_frac ~0.9) following the North-African coast,
which runs east–west along the bottom of the frame — i.e. *parallel* to
the direction of travel.  The ship should keep heading WEST with the
coast on its PORT (left/south) side.

The bug (before the fix): with the coast parallel and distant, its
frame-edge exit is unstable, so the `new_anchor` fallback kept adopting
whichever end of the coast it walked to — flipping the commit to E/NE
(t114-132) with the land on STARBOARD and open water on port.  For a
port-hugger that's an illegal hug-side flip; the ship zig-zagged back
east instead of continuing west.

The fix: a hug-side invariant guard.  `_hug_side_has_land` casts a ray
to the port side; if it finds only open water while the far side has the
bank (`not _far_side_open`), the coast is on the wrong side and the
tactical HOLDs the last hug-valid commit (keeps travelling the way that
kept land on port) instead of doubling back.

Expected behavior (with the fix): the commit stays in the SW–W–S arc
with the coast on port; `hold(hug_side,port)` intercepts the flip
attempts, and hug-side violations stay ~0 (vs many before).
"""
from .scenario import Scenario

SCENARIO = Scenario(
    name="med_hug_west",
    session="med_hug_west_t114",
    start_tick=108,
    end_tick=150,
    initial_commit_deg=288.0,          # WEST — following the coast west
    hug_side="port",
    initial_wp_offset=(0, -90),        # pre-seed the LEFT (west) coast anchor
    initial_wp_edge="left",
    # The invariant: the coast must stay on the PORT side.  Before the fix
    # the commit flipped E/NE with land on starboard for ~15+ ticks.
    max_hug_violations=4,
    note="Open-Med hug-side persistence: E–W coast parallel to travel; the "
         "commit must not flip so the coast ends up on starboard — hold the "
         "last hug-valid (westward) course instead of doubling back east.",
)
