"""Nubia bend scenario — sticky-anchor stress test.

At t176 of session ai_nav_2026-07-27T15-53-21, the ship has been
sailing south through a narrow reach, WP consistently on bot edge
via sticky-anchor for 170+ ticks.  Around t176, the bot-edge water
run disappears (river bends east; the exit no longer touches the
bot frame edge).  sticky-anchor correctly reports "anchor gone"
and falls through to new_anchor.

Tricky properties:
  - The topology transition is a legitimate river bend, not a bug
  - Fresh trace at t176 has to pick a NEW anchor that's not 180°
    opposite of the prev one (P17-class flip trap)
  - Under earlier code, new_anchor adopted a TOP-edge endpoint —
    turned the ship 180° north.  Now with fresh-per-tick pick,
    new_anchor should adopt a RIGHT-edge endpoint (~55° from prev,
    matches the river's east bend)

Expected:
  - Sticky(bot) while the bot run persists, then a validated
    migrate(bot→right) at ~t172 (the river's east bend)
  - The anchor MUST end up on the right edge — a dead-end-hold that
    pins the anchor on bot and misses the exit is the regression this
    guards (dominant_edge="right" catches it; the old loose bearing
    range did not)
  - No flip to top edge, no 180°-class direction reversal

Trimmed to t180: the bot→right migration completes by ~t173, so t180
gives the right edge a clear majority while ending soon after the
flagged transition.
"""
from .scenario import Scenario

SCENARIO = Scenario(
    name="nubia_bend",
    session="ai_nav_2026-07-27T15-53-21",
    start_tick=167,
    end_tick=180,
    initial_commit_deg=140.0,     # SE, ~ship's real heading approaching the bend
    hug_side="port",
    # Pre-seed sticky(bot) so the scenario tests the TRANSITION at
    # t172 (bot run disappears), not cold-start bootstrap at t167.
    # WP offset (93, 50) is the recorded WP at t166 — the tick BEFORE
    # start_tick — which becomes the "prev" state for t167's
    # sticky-anchor lookup.
    initial_wp_offset=(93, 50),
    initial_wp_edge="bot",
    max_flips_90deg=1,             # migration at ~t172 is ~40°, not a flip
    max_bootstrap_count=3,         # the t169 new_anchor + a couple ok
    expected_dominant_edge="right",  # the ship MUST take the east exit
    expected_wp_bearing_range=(60.0, 240.0),  # south OR east, but not north
    note="Nubia-area river bend; anchor must migrate bot→right to the "
         "east exit (not hold on bot)",
)
