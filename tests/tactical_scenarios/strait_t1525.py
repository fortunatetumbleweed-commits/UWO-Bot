"""[REDESIGN TARGET / xfail] Narrow strait — no clean through-direction.

t1516-1537 of session ai_nav_2026-07-30T20-39-30 (trimmed into
`strait_t1525`).  The ship is in a narrow strait (Messina-like, ~lat 38 /
lon 15.6): a large landmass to the WEST (a peninsula) and another to the
EAST, water between.  Heading north on the way up the Med, it needs to
pass THROUGH the strait, but the port-hug follows the west peninsula and
turns the ship WEST off the through-route; the anchor flips between the
top (N) and bottom (S) exits and the ship gets stuck (position frozen).

Current logic: starts N (sticky top) then migrates/sticks LEFT and drifts
west — the commit spans N→W, it never commits to the through-direction.
This exposes the core gap: the tactical is purely LOCAL (follow a bank)
and has no first-class DESIRED TRAVEL DIRECTION to pick the strait exit
that makes progress toward the goal.

xfail until the hugging / desired-direction redesign lands.
"""
from .scenario import Scenario

SCENARIO = Scenario(
    name="strait_t1525",
    session="strait_t1525",
    start_tick=1520,
    end_tick=1536,
    initial_commit_deg=340.0,          # NORTH — heading up the Med
    hug_side="port",
    # A ship going through a strait should hold a consistent through-heading.
    # Current logic spans N→W (~84°); a clean pass holds within ~60°.
    max_commit_spread_deg=60.0,
    xfail=True,
    xfail_reason="strait: local bank-hug turns the ship W off the through-"
                 "route instead of holding N through the strait — needs the "
                 "desired-direction redesign.",
    note="Narrow strait, land on both sides — the port-hug diverges from the "
         "through-direction and the ship stalls.",
)
