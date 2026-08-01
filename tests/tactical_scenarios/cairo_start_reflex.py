"""Cairo departure — L3 reflex-WP goalward regression.

Same wide-water Cairo departure family as `cairo_start`, but this
scenario asserts the reflex waypoint the planner emits stays goalward.
It exists because the other scenarios inspect the tactical *anchor*
(`_current_dest_px_offset`), not the reflex WP the planner picks from
the trace — so they are blind to reflex regressions.

Guards the regression from session ai_nav_2026-07-28T12-29-22 (a live
port-hug run): the reflex picker followed a noisy north-going open-water
bank trace and flipped the reflex WP to the top frame edge (t4:
wp=[2,164], bearing 340° while commit was ~186° south), which fed back
into the next trace-start and spiralled the ship near Cairo.

Trimmed to t6 (the flag onset is t3-4): the frames past that are the
recorded SPIRAL — the buggy trajectory the fixed bot wouldn't fly — so
asserting on them would validate against a bad path.  Over the clean
departure window the good picker keeps the reflex goalward (0 misaligned
ticks); the buggy arc-length picker misfires at t3 and t4 (2 ticks), so
threshold 0 catches it with no post-problem frames.
"""
from .scenario import Scenario

SCENARIO = Scenario(
    name="cairo_start_reflex",
    session="ai_nav_2026-07-28T12-29-22",
    start_tick=1,
    end_tick=6,
    initial_commit_deg=180.0,     # south — matches the run's --commit-bearing 180
    hug_side="port",
    max_reflex_misaligned=0,       # good picker = 0; buggy picker misfires at t3,t4
    reflex_misalign_thr_deg=90.0,  # reflex >90° off commit = pointing away from goal
    note="Cairo departure; reflex WP must stay goalward in noisy open water",
)
