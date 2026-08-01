"""Nile meander (return leg): the hug-side rule must defer to the anchor.

t572-595 of session ai_nav_2026-07-30T20-39-30 (trimmed into
`nile_meander_t582`).  On the northbound return leg the ship follows a
tight S-bend in the narrow Nile (water_frac ~0.17).  The channel bends,
and at the bend the inner bank transiently leaves open water on one side
— which *looks* like the open-water "coast on the wrong side" case, but
isn't: it's a channel, and the sticky/migrate anchor already owns the
travel direction (the world-locked edge exit).

The bug (before the fix): the hug-side rule (an open-water device) fired
here, froze the anchor at the top-left for ~7 ticks (hold(hug_side)), and
when it finally released the geometry had shifted and `new_anchor` grabbed
the wrong (bottom) exit — a reversal.  The tactical WP jumped to a bottom
edge instead of following the bend.

The fix: the hug-side rule only engages when the anchor ISN'T giving a
reliable direction — i.e. the cascade re-picked from scratch
(new_anchor / bootstrap).  When it produced a TRACKED anchor
(sticky / migrate / hold), that direction is trusted (river / narrow /
island-gap all rely on the anchor).  So here migrate(top→left) proceeds
and the bend is followed; the hug-side rule never overrides.

Expected behavior (with the fix): migrate(top→left) at the bend, then
sticky tracking — ZERO hug-side overrides (no hold(hug_side), no
hug_repick).  Before the fix this scenario shows ~7 hug-holds.
"""
from .scenario import Scenario

SCENARIO = Scenario(
    name="nile_meander",
    session="nile_meander_t582",
    start_tick=575,
    end_tick=594,
    initial_commit_deg=359.0,          # NORTH — return leg heading up the Nile
    hug_side="port",
    initial_wp_offset=(-93, -2),       # pre-seed the TOP (upstream) anchor
    initial_wp_edge="top",
    # The invariant: in a channel the anchor owns direction; the hug-side
    # rule must NOT override it (it misfires at the meander bend).
    max_hug_overrides=0,
    note="Nile meander on the return leg: the hug-side (open-water) rule must "
         "defer to the sticky/migrate anchor in the channel — 0 overrides.",
)
