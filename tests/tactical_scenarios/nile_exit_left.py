"""Nile→Med exit: coast bends WEST, anchor must migrate top→left.

t794-806 of session ai_nav_2026-07-30T15-06-06 (trimmed into
`nile_exit_left_t796`).  Sailing north OUT of the Nile toward the
Mediterranean, the port (left) bank curves west: the bank-tracer exit
slides along the top edge leftward (t788-795) and by t796 reaches the
top-LEFT corner, then descends the LEFT edge (t796-806) as the coast
runs off to the west.  The ship should turn LEFT and follow it.

Two bugs blocked that (both fixed here):
  1. Edge classification checked top>bot>left>right by priority, so the
     corner endpoint (3, 0) — y=3 AND x=0 both within the margin — was
     labelled "top", matching prev_edge, so `sticky(top)` clung forever
     and the leftward turn was never even seen.  Fixed by classifying to
     the CLOSEST edge (x=0 < y=3 → "left").
  2. Even once seen as "left", the `migration_route_ok` guard (trace must
     pass ≤60 px of the OLD top-centre anchor) permanently blocked the
     migration: in a coast bend the ship follows the bank away from the
     old anchor, so the trace diverges from it (64→119 px).  Fixed by
     also accepting a migration whose endpoint arrived CONTINUOUSLY
     (≤40 px tick-over-tick after frame-shift) — a smooth bend, not the
     discontinuous leap the artifact_reversal guard catches.

Expected behavior (with the fixes):
  - sticky(top) while the ship still climbs the channel (t794-795)
  - sticky(top,trace_noise) for 2 ticks while the left-edge exit's
    persistence builds (t796-797)
  - migrate(top→left) once persistence hits 3 (t798), commit swings
    from ~0° (north) to ~295° (WNW, a left turn)
  - sticky(left) LOCKS thereafter (t799-806), commit tracking the coast
    curving west (~282-294°).  Dominant edge = LEFT, not the top edge it
    was stuck on before the fix.
"""
from .scenario import Scenario

SCENARIO = Scenario(
    name="nile_exit_left",
    session="nile_exit_left_t796",
    start_tick=794,
    end_tick=806,
    initial_commit_deg=0.0,            # NORTH — sailing out of the Nile
    hug_side="port",
    initial_wp_offset=(-93, 10),       # pre-seed the TOP (channel-north) anchor
    initial_wp_edge="top",
    # The invariant: the anchor must migrate onto and LOCK to the LEFT edge
    # (the coast bending west), not stay stuck on the top edge going north.
    expected_dominant_edge="left",
    # And the WP must actually point WNW (the left turn) for the majority of
    # ticks, not north.
    expected_wp_bearing_range=(255.0, 315.0),
    note="Nile→Med coast bend: port bank curves west; anchor must migrate "
         "top→left (closest-edge classify + endpoint-continuity route relax) "
         "and lock on the left edge, turning the ship WNW.",
)
