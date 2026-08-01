"""Open-water coast-follow scenario.

t918-t966 of session ai_nav_2026-07-30T13-29-08 (trimmed into
`open_water_coast_t930`).  After overshooting Cairo the ship is in the
open Med, heading WEST with the coast running along the BOTTOM edge and
open sea on the far (starboard/north) side.

The bug (before the fix): the anchor policy's `edge_is_recently_new`
gate — which correctly rejects a persistent side edge as "port-hug cliff"
in a bounded Nile channel — also rejects the persistent COAST in open
water, so `sticky(left, trace_noise)` clung to the open-SEA edge, called
the real coast "noise", and the ship sailed straight off the coast into
open water (land gone by ~t976).

The fix: `_far_side_open` detects the open-water regime (far side is sea
to the frame edge, not a bounded channel), and a `coast_follow` branch
then adopts the persistent bank-tracer exit on the coast edge instead of
holding the sea edge.

Expected behavior (with the fix):
  - sticky(left) briefly while the trace-diff builds persistence
  - coast_follow(left→bot) migrates the anchor onto the coast, commit
    turns PORT (271°→239°)
  - sticky(bot) LOCKS on the coast thereafter, commit keeps turning port
    to follow it curving away — the anchor's dominant edge is BOTTOM,
    NOT the open-sea LEFT edge it was stuck on.
"""
from .scenario import Scenario

SCENARIO = Scenario(
    name="open_water",
    session="open_water_coast_t930",
    start_tick=926,
    end_tick=945,
    initial_commit_deg=271.0,          # WEST — the stuck sail-along-sea commit
    hug_side="port",
    initial_wp_offset=(0, -197),       # pre-seed the LEFT (open-sea) edge anchor
    initial_wp_edge="left",
    # The invariant: the anchor must end up LOCKED on the coast (bottom edge),
    # not stuck on the open-sea (left) edge.  Without the coast-follow fix this
    # is dominantly "left" and fails.
    expected_dominant_edge="bot",
    note="Open-water coast-follow: far side is sea, so a persistent bottom-edge "
         "coast must be followed (coast_follow left→bot) not held as trace_noise.",
)
