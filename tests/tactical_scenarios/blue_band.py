"""Bright-glare-band artifact scenario (transient channel severance).

t714-t724 of session ai_nav_2026-07-29T15-42-01, trimmed into
`blue_band_glare_t721`.  The ship is on the RETURN leg heading NORTH
(commit ~350°, sticky(top)) up a narrow channel.  At t721 a bright
diagonal sun-glare band crosses the minimap; it's too bright for the
V11 `brightness<120` water test, so it's classified as LAND and
SEVERS the northern channel.  The bank tracer then can't sticky/migrate
(old north edge gone, not persistent) and falls to a cold `new_anchor`
that adopts the only remaining connected water — the SOUTH exit —
flipping commit 348°→179° in one tick and (before the fix) permanently
reversing the return leg.

The band is a 1-tick transient.  The artifact-reversal guard should
HOLD the old north anchor through it: a `new_anchor` that reverses the
commit >90° AND whose trace doesn't pass near the old WP is treated as a
likely artifact and held for a few ticks; the channel returns at t722
and sticky(top) resumes.

Expected behavior (with the fix):
  - sticky(top) north on the approach (t716-720)
  - hold(artifact_reversal,top) at t721 — commit stays ~north, NOT flipped
  - sticky(top) resumes t722+ once the band clears
  - NO ~180° commit flip
"""
from .scenario import Scenario

SCENARIO = Scenario(
    name="blue_band",
    session="blue_band_glare_t721",
    start_tick=716,
    end_tick=724,
    initial_commit_deg=356.0,          # NORTH — the return-leg commit at t716
    hug_side="port",
    initial_wp_offset=(-93, 10),       # t716 anchor on the TOP edge (north)
    initial_wp_edge="top",
    # The bug: the glare band flipped commit ~169° in one tick.  The guard
    # must keep every single-tick commit rotation small (no anchor flip).
    max_commit_rotations=0,
    commit_rotation_thr_deg=60.0,
    # And the ship must stay committed NORTH the whole clip (no reversal).
    expected_dominant_edge="top",
    note="Transient bright glare band severs the north channel at t721; "
         "artifact-reversal guard must hold the north anchor, not flip south.",
)
