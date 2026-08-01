"""Y-fork approach from north — sticky-anchor stress at a junction.

t371-t393 of session ai_nav_2026-07-25T11-01-09.  Ship approaches
a Y-fork in the river from the north.  In the live voyage, the WP
correctly followed the bot-edge exit through t369-t390 (curving
east with the river bend), then FLIPPED at t391 to a LEFT-edge
anchor (72, -196) — the classic P16/P17 cliff.  Ship got stuck
on wrong branch and voyage failed.

Tricky properties:
  - Multiple frame-edge water runs exist simultaneously (Y-fork
    has bot AND left AND right exits at various points)
  - Water topology shifts as ship passes the fork
  - Small mask perturbation at t391 flipped the fresh trace's
    port-hug walker to the LEFT branch

Expected behavior with sticky-anchor + fresh-per-tick pick:
  - Sticky(bot) throughout t371-t390 as the WP tracks the bot-run
    midpoint moving east
  - t391 either continues sticky(bot) OR transitions cleanly to
    sticky(right) if the bot exit legitimately closes
  - No flip to LEFT edge (~180° opposite the approach direction)
"""
from .scenario import Scenario

SCENARIO = Scenario(
    name="y_fork",
    session="ai_nav_2026-07-25T11-01-09",
    start_tick=371,
    end_tick=393,
    initial_commit_deg=223.0,     # SSW, ship's real commit at t371
    hug_side="port",
    # Pre-seed: at t370 the WP was (93, -70) on bot edge — that
    # becomes the "prev" for t371's sticky lookup.
    initial_wp_offset=(93, -70),
    initial_wp_edge="bot",
    max_flips_90deg=1,             # bot → right transition allowed (~90°)
    max_bootstrap_count=3,
    expected_dominant_edge=None,    # bot + possibly right, no single dominant
    expected_wp_bearing_range=(90.0, 260.0),  # south / east / SW ok, NW/N not
    note="Y-fork river approach from north; t391 was the flip point "
         "in the original P16-class failure",
)
