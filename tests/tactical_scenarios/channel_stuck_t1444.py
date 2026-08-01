"""[REDESIGN TARGET / xfail] Open-Med narrows — ship can't commit, stalls.

t1428-1452 of session ai_nav_2026-07-30T20-39-30 (trimmed into
`channel_stuck_t1444`).  In the open Med around lat 37 / lon 12 (the
Sicily–Tunisia narrows) the ship stalls (position frozen) while the
commit drifts across HALF the compass: W → SW → S → E in one window,
via sticky(left) → coast_follow → hold(pocket) → new_anchor(bot) →
new_anchor(right).  It's going both WEST and EAST — it cannot commit to a
consistent direction and makes no progress.

Same root cause as `strait_t1525`: the tactical is a local bank-follower
with no first-class desired travel direction, so among the several
land/opening configurations here it keeps re-picking a different anchor
and oscillates.

xfail until the hugging / desired-direction redesign lands.
"""
from .scenario import Scenario

SCENARIO = Scenario(
    name="channel_stuck_t1444",
    session="channel_stuck_t1444",
    start_tick=1432,
    end_tick=1451,
    initial_commit_deg=291.0,          # WEST — as it entered the narrows
    hug_side="port",
    # The commit spans ~168° here (W and E both) — nowhere near a held
    # direction.  A clean pass keeps it within ~60°.
    max_commit_spread_deg=60.0,
    xfail=True,
    xfail_reason="open-Med narrows: commit oscillates W↔E, no consistent "
                 "direction — needs the desired-direction redesign.",
    note="Open-Med narrows — ship stalls, commit drifts across half the "
         "compass (W→SW→S→E) instead of committing to a through-heading.",
)
