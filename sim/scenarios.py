"""Named regression scenarios from live failure logs.

Each scenario captures a specific failure mode we observed in a live
run, with the input observation (8 sector readings) and the expected
behavior.  These are CONCRETE scenarios in PEGASUS terminology —
specific instances, not parameterised templates.

Discipline:
    - Don't delete scenarios.  Each represents a bug that was fixed.
    - When adding a fix, write the scenario FIRST, watch it fail, then
      make it pass.
    - When refactoring, scenarios must keep passing.  If a refactor
      makes a scenario semantically obsolete, document why in the
      scenario's `note=` field.

See `docs/hug_shore_scenarios.md` for the test plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal, Optional, Tuple

from vision.navigation_view import SectorReading


# ── Scenario dataclass ──────────────────────────────────────────────

ExpectedActionCategory = Literal[
    "hold",                # action == "hold"
    "turn_target",         # action turns toward target side
    "turn_away",           # action turns away from target side
    "escape",              # action has "ESC" marker (dead-end escape)
    "any_non_hold",        # any non-hold action (e.g. lost-shore recovery)
]


@dataclass
class Scenario:
    """One concrete scenario from a live failure log."""
    name: str
    side: Literal["port", "starboard"]
    # 8 sector readings.  Each is (frac, dist|None).  Order matches
    # NavigationView: 0=ahead, 1=ahead-stbd, 2=stbd, 3=astern-stbd,
    # 4=astern, 5=astern-port, 6=port, 7=ahead-port.
    sectors: Tuple[Tuple[float, Optional[float]], ...]
    ship_heading_deg: float = 90.0
    expected_action: ExpectedActionCategory = "hold"
    # Optional: pre-set HugShoreGoal state before running tick.
    last_chosen_sector: Optional[int] = None
    last_ideal_sector: Optional[int] = None
    consecutive_blocked: int = 0
    wall_distance: float = 0.20
    # Optional: prior tick's sector readings, primes goal.history so
    # velocity signals (Δfrac, Δdist) can be derived for this tick.
    prior_sectors: Optional[Tuple[Tuple[float, Optional[float]], ...]] = None
    # Free-form context — what the live run looked like, why this
    # scenario exists, what the expected behavior means physically.
    note: str = ""
    # Source: log timestamp or live-run reference
    source: str = ""

    def to_navigation_view(self):
        """Build a NavigationView-shaped object from this scenario."""
        sector_readings = tuple(
            SectorReading(
                bearing_deg=float(i * 45),
                land_fraction=frac,
                nearest_dist=dist,
                is_observed=True,
            )
            for i, (frac, dist) in enumerate(self.sectors)
        )

        class _ScenarioNavView:
            __slots__ = ("ship_heading_deg", "sectors")

        v = _ScenarioNavView()
        v.ship_heading_deg = self.ship_heading_deg
        v.sectors = sector_readings
        return v


# ── Regression scenarios drawn from live logs ───────────────────────
#
# Format: each `Scenario(...)` is one failure case we want pinned
# forever.  Add new ones at the end with a fresh dated source tag.

LOGGED_FAILURES: list[Scenario] = [
    Scenario(
        name="tripoli_intrusion",
        source="2026-05-28 ~15:10",
        side="starboard",
        ship_heading_deg=80.7,
        sectors=(
            (0.00, 0.07),    # ahead — sliver close (edge bleed)
            (0.31, 0.06),    # bT
            (0.97, 0.03),    # T — heavy shore
            (0.00, None),
            (0.00, None),
            (0.00, None),
            (0.02, 0.20),    # O
            (0.02, 0.29),    # bO
        ),
        expected_action="hold",
        note=(
            "Ship just departed Tripoli, shore RIGHT next to right beam. "
            "Pre-fix bug: bot peeled left because ahead.dist=0.07 read "
            "as 'obstacle'.  Post-fix: intrusion discount + ahead.frac=0 "
            "→ sector 0 cost ~0 → HOLD."
        ),
    ),

    Scenario(
        name="corner_with_real_wall_ahead",
        source="2026-05-28 ~15:32",
        side="starboard",
        ship_heading_deg=80.7,
        sectors=(
            (0.11, 0.04),    # ahead — REAL wall close (frac > 0.05)
            (0.40, 0.03),    # bT
            (0.88, 0.03),    # T
            (0.00, None),
            (0.00, None),
            (0.00, None),
            (0.07, 0.06),    # O — close on left too
            (0.07, 0.06),    # bO
        ),
        expected_action="any_non_hold",  # would settle on hold for first 2 ticks,
                                          # but with consecutive_blocked=3 → escape
        consecutive_blocked=3,
        note=(
            "Corner / dead end.  Pre-fix bug: intrusion fix discounted "
            "ahead proximity and bot held for 8 ticks grinding forward. "
            "Post-fix: intrusion check requires ahead.frac<0.05 (this is "
            "0.11) AND ahead.dist >= 0.06 (this is 0.04) — both fail. "
            "With consecutive_blocked=3, dead-end escape fires."
        ),
    ),

    Scenario(
        name="river_channel_opens_right",
        source="2026-05-28 ~12:50",
        side="starboard",
        ship_heading_deg=122.9,
        sectors=(
            (0.59, 0.07),    # ahead
            (0.18, 0.10),    # bT — clearer bow → channel direction
            (0.94, 0.07),    # T
            (0.00, None),
            (0.00, None),
            (0.00, None),
            (0.89, 0.07),    # O
            (0.67, 0.06),    # bO — loaded
        ),
        expected_action="turn_target",   # turn right toward clearer bow
        note=(
            "Narrow river channel.  Right bow (bT=0.18) much clearer "
            "than left bow (bO=0.67).  Channel opens to the right; "
            "bot should turn right.  Pre-fix: beam nearest_dist tied "
            "and policy picked left.  Post-fix: bow.land_fraction picks "
            "the clearer side."
        ),
    ),

    Scenario(
        name="close_T_rotation_hazard",
        source="2026-05-28 ~16:19",
        side="starboard",
        ship_heading_deg=322.6,
        sectors=(
            (0.01, 0.11),    # ahead
            (0.02, 0.14),    # bT — looks clean
            (0.14, 0.08),    # T — close shore right beam
            (0.00, None),
            (0.00, None),
            (0.00, None),
            (0.10, 0.13),    # O
            (0.02, 0.07),    # bO
        ),
        expected_action="hold",
        note=(
            "Pre-fix bug: bot turned right toward bT because bT.dist=0.14 "
            "looked clean.  But T.dist=0.08 (close shore right beam) "
            "means turning right would rotate close-T into bow — "
            "scraping our own hug-shore.  Post-fix: rotation penalty "
            "on sector 1 when T.dist < PROXIMITY_DANGER."
        ),
    ),

    Scenario(
        name="facing_wall_peel_via_hysteresis_off",
        source="2026-05-28 ~19:18 log tick 20",
        side="starboard",
        ship_heading_deg=169.3,
        sectors=(
            (0.64, 0.06),   # ahead — REAL wall close
            (0.78, 0.04),   # bT — HEAVY shore right bow (curving in)
            (0.24, 0.07),   # T — shore right beam
            (0.00, None),
            (0.00, None),
            (0.00, None),
            (0.01, 0.09),   # O — CLEAR left beam = the obvious peel direction
            (0.50, 0.05),   # bO — left bow loaded
        ),
        last_chosen_sector=0,        # came from previous tick where 0 was cheapest
        consecutive_blocked=1,        # already 1 BLOCKED tick, not yet at escape
        expected_action="turn_away",  # turn LEFT toward sector 6 immediately
        note=(
            "Ship facing south at Tripoli coast curving south.  Real "
            "wall ahead (0.64/0.06), shore curving onto bow-right (0.78), "
            "but LEFT beam clear (O=0.01).  Sector 6 cost (0.59) is "
            "cheaper than sector 0 (0.64), but hysteresis margin 0.10 "
            "swallowed the 0.05 difference and kept the wall-sector.  "
            "Pre-fix: bot held for 3 ticks grinding into wall until "
            "dead-end escape fired.  Post-fix: hysteresis is suppressed "
            "when last_chosen_sector has wall-level obstacle cost (>=0.50)."
        ),
    ),

    Scenario(
        name="dead_end_escape_prefers_target_side",
        source="2026-05-28 ~19:55 log tick 3",
        side="starboard",
        ship_heading_deg=345.3,
        sectors=(
            (0.03, 0.05),    # ahead — close land
            (0.03, 0.05),    # bT — close land right bow
            (0.00, 0.03),    # T — close shore right beam
            (0.00, None),
            (0.00, None),
            (0.00, None),
            (0.26, 0.08),    # O — moderate land on left
            (0.01, 0.04),    # bO — close land left bow
        ),
        consecutive_blocked=3,        # at the threshold; escape fires
        expected_action="turn_target", # turn RIGHT toward target side
        note=(
            "Surrounded by close land on all sides at start (in tight "
            "quarters by harbor).  Cost dump: [6=0.91 7=1.05 0=0.64 "
            "1=1.01 2=1.00].  Cheapest non-zero is sector 6 (left) by "
            "0.09 over sector 2 (right).  Pre-fix: escape picked sector "
            "6 → bot turned LEFT, away from where shore should be on a "
            "starboard hugger.  Post-fix: when target-side alternative "
            "is within ESCAPE_TARGET_BIAS of cheapest, prefer it — "
            "keeps the bot pointed toward where shore is 'supposed' to "
            "be during emergencies."
        ),
    ),

    Scenario(
        name="wall_ahead_only_peels_left",
        source="2026-05-28 probe A1 (synthetic, user-specified rule)",
        side="starboard",
        ship_heading_deg=0.0,
        sectors=(
            (0.50, 0.06),    # ahead — wall
            (0.00, None),    # bT — clear
            (0.00, None),    # T — clear
            (0.00, None),
            (0.00, None),
            (0.00, None),
            (0.00, None),    # O — clear
            (0.00, None),    # bO — clear
        ),
        expected_action="turn_away",  # peel LEFT (away from target side)
        note=(
            "Wall directly ahead, every other direction clear.  "
            "Classic right-hand-on-the-wall: stbd hugger peels LEFT.  "
            "Pre-fix: ideal=1 (recovery toward target) pulled bot to "
            "sector 1 (right) → bot turned right into the wall side.  "
            "Post-fix: wall-follow override sets ideal=7 (ahead-port) "
            "when ahead is walled and opposite-beam is clear."
        ),
    ),

    Scenario(
        name="peninsula_tip_peels_left_when_left_safer",
        source="2026-05-28 probe B2 (synthetic)",
        side="starboard",
        ship_heading_deg=0.0,
        sectors=(
            (0.50, 0.06),    # ahead — wall
            (0.50, 0.04),    # bT — RIGHT bow heavier
            (0.00, None),    # T — clear
            (0.00, None),
            (0.00, None),
            (0.00, None),
            (0.00, None),    # O — clear
            (0.50, 0.06),    # bO — left bow loaded but lighter than bT
        ),
        expected_action="turn_away",  # peel LEFT — right is genuinely worse
        note=(
            "Peninsula tip with right-bow MORE dangerous than left-bow. "
            "Pre-fix: ideal=1 right-bias caused bot to pick sector 2 "
            "(right beam) — going right when right is the worse side! "
            "Post-fix: wall-follow override → sector 6 (left beam)."
        ),
    ),

    Scenario(
        name="close_T_low_frac_intrusion",
        source="2026-05-28 ~21:06 log tick 1",
        side="starboard",
        ship_heading_deg=346.6,
        sectors=(
            (0.04, 0.07),    # ahead — close, but likely shore-edge
            (0.03, 0.04),    # bT — close (shore extending into bow)
            (0.06, 0.03),    # T — small frac but VERY close (shore right there)
            (0.00, None),
            (0.00, None),
            (0.00, None),
            (0.23, 0.09),    # O
            (0.00, 0.17),    # bO
        ),
        expected_action="hold",
        note=(
            "Ship in tight harbor mouth at Tripoli, shore JUST clipping "
            "right beam (T.dist=0.03 — almost touching, but only 6% of "
            "the beam sector has it).  The close ahead reading (0.07) "
            "is the same landmass intruding into the ahead boundary.  "
            "Pre-fix: intrusion required T.frac>=0.70 → didn't fire → "
            "bot turned LEFT 45° to avoid 'ahead obstacle'.  Post-fix: "
            "intrusion ALSO fires when T.dist < 0.05 regardless of "
            "frac — shore close enough to touch the beam is shore."
        ),
    ),

    Scenario(
        name="wrong_side_u_turn",
        source="2026-05-28 synthetic (Tripoli circle pattern root cause)",
        side="starboard",
        ship_heading_deg=0.0,
        sectors=(
            (0.00, None),    # ahead — clear
            (0.00, None),    # bT — clear (target side empty)
            (0.00, None),    # T  — clear (target side empty)
            (0.00, None),
            (0.00, None),
            (0.00, None),
            (0.40, 0.10),    # O — shore on OPPOSITE side
            (0.30, 0.15),    # bO — opposite-bow has shore too
        ),
        expected_action="turn_away",   # peel LEFT toward opp-shore, begin U-turn
        note=(
            "Bot has shore visible only on the OPPOSITE side (left for "
            "stbd hugger).  This is the wrong-side trap that drove "
            "the circular pattern at Tripoli and the lost-shore drift "
            "near the Nile.  Pre-fix: ideal=1 (lost-shore recovery) "
            "made bot swing right — AWAY from the only shore visible. "
            "Post-fix: target_empty AND opp_has_shore → ideal=7, bot "
            "peels left toward the opposite shore.  Combined with the "
            "wall-follow override during rotation, this initiates a "
            "natural U-turn that ends with shore on target side."
        ),
    ),

    Scenario(
        name="lost_shore_recovery",
        source="multiple runs",
        side="starboard",
        ship_heading_deg=0.0,
        sectors=(
            (0.00, None),
            (0.00, None),
            (0.00, None),
            (0.00, None),
            (0.00, None),
            (0.00, None),
            (0.00, None),
            (0.00, None),
        ),
        expected_action="turn_target",  # ideal=1 → recover toward target
        note=(
            "Pure open water, no shore anywhere.  Recovery behavior: "
            "ideal sector flips to AHEAD_TARGET, virtual wall on "
            "opposite side, bot turns right (for starboard) to find "
            "shore."
        ),
    ),

    Scenario(
        name="wrong_side_with_shore_on_astern_opposite",
        source="live tick 117 of 2026-05-29_145608 run",
        side="starboard",
        ship_heading_deg=254.0,
        sectors=(
            (0.00, 0.64),  # A — clear ahead
            (0.01, 0.10),  # bS — clear bow-target
            (0.02, 0.16),  # S — empty target
            (0.02, 0.19),  # aS
            (0.03, 0.34),  # B
            (0.31, 0.34),  # aP — substantial shore astern-port (the trap)
            (0.00, 0.49),  # P — clear port-beam
            (0.00, None),  # bP — clear bow-port
        ),
        expected_action="turn_away",  # peel toward bow-port → start U-turn
        note=(
            "Post-overshoot wrong-side: after an over-rotated left turn "
            "(t114 commanded -90°, actual -141°), shore was sitting on "
            "astern-port.  Old detector only checked port-beam and "
            "bow-port, both 0.00 — wrong-side missed for 7 ticks while "
            "the bot drifted.  Post-fix: astern-opposite is checked → "
            "aP=0.31 satisfies opp_has_shore → ideal=bow-port → U-turn."
        ),
    ),

    Scenario(
        name="wrong_side_with_tiny_target_sliver",
        source="live tick 1 of 2026-05-29_143711 run",
        side="starboard",
        ship_heading_deg=332.0,
        sectors=(
            (0.03, 0.27),  # A
            (0.02, 0.27),  # bS — clear bow-target
            (0.01, 0.07),  # S — TINY frac, close dist (the trap)
            (0.10, 0.20),  # aS
            (0.20, 0.15),  # B
            (0.50, 0.20),  # aP — substantial shore astern-port
            (0.83, 0.12),  # P — heavy close shore on port (wrong side)
            (0.31, 0.24),  # bP — moderate shore on bow-port
        ),
        expected_action="turn_away",  # peel toward bow-port to start U-turn
        note=(
            "Live wrong-side trap: shore is heavily on port (P=0.83/0.12) "
            "but the bot stayed HUGGING because target.dist=0.07 made "
            "target_empty=False.  Tiny S.frac=0.01 was treated as "
            "'shore present' due to the close-distance reading.  "
            "Post-fix: WRONG_SIDE_TINY_FRAC=0.05 makes target.frac<0.05 "
            "count as empty regardless of distance → wrong-side detected → "
            "ideal=bow-port → U-turn starts."
        ),
    ),

    Scenario(
        name="bow_target_close_loaded_peels_early",
        source="live tick 14 of 2026-05-29_133115 run",
        side="starboard",
        ship_heading_deg=148.0,
        sectors=(
            (0.08, 0.31),  # A — barely loaded; below AHEAD_WALL_FRAC
            (0.53, 0.28),  # bS — close + loaded (the new trigger)
            (0.67, 0.33),  # S — shore visible on target beam
            (0.25, 0.53),  # aS
            (0.02, 0.42),  # B
            (0.02, 0.42),  # aP
            (0.01, 0.51),  # P — opp-beam clear (allows peel)
            (0.00, 0.58),  # bP — opp-bow clear
        ),
        expected_action="turn_away",
        note=(
            "Live collision-approach signature: shore is closing in on "
            "the bow-target sector (bS=0.53/0.28) but A.frac is still "
            "0.08, well below AHEAD_WALL_FRAC=0.30.  Without this "
            "trigger, the bot held while the coast curved inward and "
            "collided 2 ticks later.  Post-fix: bow_t_close_loaded "
            "trigger fires → wall-follow override → peel away."
        ),
    ),

    Scenario(
        name="bow_target_velocity_peels_before_static_threshold",
        source="live tick 10→11 of 2026-05-29_165018 run",
        side="starboard",
        ship_heading_deg=88.0,
        # Tick 10 — bS at safe distance, frac negligible.
        prior_sectors=(
            (0.00, 0.50),  # A
            (0.02, 1.00),  # bS — clear
            (0.10, 0.60),  # S
            (0.05, 0.70),  # aS
            (0.00, 1.00),  # B
            (0.00, 1.00),  # aP
            (0.00, 1.00),  # P
            (0.00, 1.00),  # bP
        ),
        # Tick 11 — bS jumped (0.02→0.25) and dist crashed (1.00→0.07).
        # Static BOW_T_CLOSE_FRAC=0.40 doesn't fire (0.25 < 0.40).  But
        # the velocity reading (Δfrac=+0.23, Δdist=-0.93) crosses the
        # APPROACH_*_DANGER thresholds → wall-follow override fires →
        # peel away.  Without the velocity trigger, the bot ran the
        # ideal-distance drift logic, picked sector 1 (bow-stbd), and
        # the cost min sat on sector 2 — rotating INTO the close shore.
        sectors=(
            (0.05, 0.40),  # A — still under AHEAD_WALL_FRAC
            (0.25, 0.07),  # bS — close + loading fast (Δfrac=+0.23, Δdist=-0.93)
            (0.30, 0.49),  # S — beam shows shore at distance
            (0.10, 0.60),  # aS
            (0.00, 1.00),  # B
            (0.00, 1.00),  # aP
            (0.01, 0.95),  # P  — opp beam clear
            (0.00, 1.00),  # bP — opp bow clear
        ),
        expected_action="turn_away",
        note=(
            "Live t11 of 2026-05-29_165018: bow-target shore was loading "
            "fast across t10→t11 (bS frac 0.02→0.25, dist 1.00→0.07) but "
            "static BOW_T_CLOSE_FRAC=0.40 didn't fire and AHEAD_WALL_FRAC "
            "didn't fire either.  Drift check then pulled ideal toward "
            "bow-target → cost function chose stbd-side → bot rotated INTO "
            "the close shore.  Post-fix: (a) APPROACH velocity trigger "
            "fires wall-follow override; (b) drift check now requires "
            "bow-target empty.  Either alone would prevent the regression."
        ),
    ),

    Scenario(
        name="astern_pull_beats_wall_follow_after_corner",
        source="live tick 29 of 2026-05-29_205124 run",
        side="starboard",
        ship_heading_deg=166.0,
        sectors=(
            (0.51, 0.27),    # A — wall ahead but distant
            (0.45, 0.35),    # bS — bow-stbd loaded, distant (NOT imminent)
            (0.83, 0.22),    # S — heavy beam-stbd shore
            (0.92, 0.16),    # aS — HEAVY astern-stbd
            (0.58, 0.09),    # B
            (0.03, 0.07),    # aP
            (0.00, 0.22),    # P — opp beam clear
            (0.01, 0.20),    # bP — opp bow clear
        ),
        expected_action="turn_target",  # astern-pull → bow-target → right
        note=(
            "Just rounded a corner: shore wraps from stbd-beam through "
            "astern (aS=0.92, S=0.83, B=0.58).  Wall-follow override "
            "USED TO fire (A.frac=0.51 ≥ 0.30, opp beam clear) → peeled "
            "LEFT into open water, abandoning the hug-line.  Astern-pull "
            "(aS=0.92 ≥ ASTERN_PULL_FRAC=0.5) would have correctly "
            "pulled toward bow-stbd to re-engage the shore.  Post-fix: "
            "astern-pull gates the wall-follow override unless bow-target "
            "is IMMINENT (frac≥0.40 AND dist<0.10).  Here bS dist=0.35 "
            "is well above 0.10, so the override is gated → astern-pull "
            "fires → ideal=bow-stbd → turn right."
        ),
    ),

    Scenario(
        name="lost_shore_tied_cost_holds_course",
        source="live tick 15 of 2026-05-29_235717 run",
        side="starboard",
        ship_heading_deg=81.0,
        sectors=(
            (0.01, 0.21),   # A — essentially empty
            (0.15, 0.42),   # bS — small distant load
            (0.00, 0.21),   # S — empty
            (0.00, 0.40),   # aS — empty
            (0.00, 0.17),   # B
            (0.03, 0.40),   # aP
            (0.04, 0.41),   # P
            (0.03, 0.42),   # bP
        ),
        expected_action="hold",
        note=(
            "Lost-shore recovery should prefer a hold-or-gentle-turn, "
            "not commit to a 90° hard turn that overshoots through "
            "where the shore used to be.  Pre-fix: ideal=bow-stbd, "
            "cost min ties sector 0 and sector 2 at 0.09 each, "
            "arbitrary tiebreak picks sector 2 → hold_right ~90° → "
            "ship rotated to heading 191°, putting the shore on port "
            "(wrong side) → triggered wrong-side U-turn → spiral.  "
            "Post-fix: W_HEADING_SMOOTH=0.02 adds 0.01 penalty to "
            "sector 2's 90° rotation while sector 0 gets 0 → sector 0 "
            "wins the previous tie → bot holds course and waits for "
            "shore to re-acquire."
        ),
    ),

    Scenario(
        name="astern_target_pulls_back",
        source="live tick 95 of 2026-05-29_113451 run",
        side="starboard",
        ship_heading_deg=103.0,
        sectors=(
            (0.00, 0.35),  # A — clear ahead
            (0.00, 0.35),  # bS — clear bow-target
            (0.00, 0.34),  # S — clear stbd beam
            (0.46, 0.30),  # aS — substantial astern-target shore!
            (0.06, 0.33),  # B
            (0.04, 0.33),  # aP
            (0.05, 0.34),  # P
            (0.02, 0.35),  # bP
        ),
        expected_action="turn_target",  # ideal must pull toward bow-target
        note=(
            "The ship sailed past its hug-line.  Forward + opposite "
            "sectors are clear; astern-target shows substantial shore "
            "(frac=0.46 at dist 0.30).  Without the astern-pull, ideal "
            "stayed at sector 0 (hold ahead) and the bot continued "
            "drifting offshore until shore was lost entirely.  With "
            "the pull, ideal flips to AHEAD_TARGET so the bot curves "
            "back into the hug."
        ),
    ),
]


# Quick lookup by name (for parametrized tests / debug).
BY_NAME: dict[str, Scenario] = {s.name: s for s in LOGGED_FAILURES}
