"""Barter quantity solver (#19) — how many rounds to barter.

A barter round consumes each input material by its ratio and yields
`output_per_round` of the good (amity-scaled). The feasible rounds are bounded by:
  • MATERIALS  — min over inputs of floor(on_hand / ratio)
  • ROUNDS     — the village's remaining daily rounds

  • SPACE      — only if we refuse overflow: cumulative net fill (output − inputs
    consumed) must fit the free hold.

By default overflow is ALLOWED and reported (in units), because carrying extra and
letting the jettison planner (#21) dump the excess is a normal play — the caller
picks. Pure + testable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence, Tuple

# The day's barter allowance at its BEST amity grade. The Base tab shows the total for the
# CURRENT grade ('Daily Barter Progress N/7'), which rises as amity does.
#
# A GAME RULE, NOT A SCREEN FACT, so it lives in the task layer. It was defined in
# `actions/village_check.py`, which meant the passive task runner could only learn the day's
# ceiling by importing a UI module — the exact reach the layering forbids.
MAX_DAILY_BARTER_ROUNDS = 7


@dataclass
class BarterPlan:
    rounds: int
    output_qty: int
    materials_consumed: dict = field(default_factory=dict)
    overflow_units: int = 0            # units that won't fit → jettison (#21) needed
    limited_by: str = "none"           # 'materials' | 'rounds' | 'space' | 'none'


def _as_pairs(inputs) -> list:
    out = []
    for it in inputs:
        if isinstance(it, (tuple, list)):
            out.append((it[0], int(it[1])))
        else:                                   # RecipeInput
            out.append((it.material, int(it.ratio)))
    return out


def solve_barter_quantity(output_per_round: int,
                          inputs,
                          materials_on_hand: Mapping[str, int],
                          rounds_remaining: int,
                          free_space: int,
                          allow_overflow: bool = True) -> BarterPlan:
    """Max feasible barter rounds under material / rounds / (optional space) limits."""
    pairs = _as_pairs(inputs)

    # Material bound.
    if pairs:
        mat_bound = min(materials_on_hand.get(m, 0) // ratio if ratio > 0 else math.inf
                        for m, ratio in pairs)
    else:
        mat_bound = math.inf

    rounds = min(mat_bound, rounds_remaining)
    limited_by = "materials" if mat_bound <= rounds_remaining else "rounds"

    consumed_per_round = sum(r for _m, r in pairs)
    net = output_per_round - consumed_per_round     # hold fill per round

    if not allow_overflow and net > 0:
        space_bound = free_space // net
        if space_bound < rounds:
            rounds, limited_by = space_bound, "space"

    rounds = int(max(0, rounds))
    if rounds == 0:
        limited_by = "materials" if mat_bound == 0 else (
            "rounds" if rounds_remaining == 0 else limited_by)

    output_qty = rounds * output_per_round
    materials_consumed = {m: rounds * r for m, r in pairs}
    total_net = rounds * net
    overflow = int(max(0, total_net - free_space)) if net > 0 else 0
    return BarterPlan(rounds, output_qty, materials_consumed, overflow, limited_by)


def output_for_amity(recipe, amity_grade: str) -> Optional[int]:
    """The recipe's per-round yield at a given amity grade (output scales with amity)."""
    if not recipe.output_per_round:
        return None
    return recipe.output_per_round.get(amity_grade)


# ── Pre-gather plan (capacity + supply aware) ─────────────────────────────────
# `solve_barter_quantity` above answers "how many rounds can I commit RIGHT NOW,
# with these materials in the hold".  The MISSION planner asks a different question
# before it has bought anything: "how many rounds should I gather for?"  That bound
# is not the net fill per round — it is the PEAK hold usage across the mission:
#
#   • on the gather → village leg the hold carries  rounds × Σ(needs)  of materials;
#   • after bartering it carries                    rounds × output    of the good.
#
# Materials are consumed as the good arrives, so the hold never holds both at once —
# but it must fit whichever is larger.  Planning on the net fill (output − inputs)
# would authorise a gather that cannot physically be carried to the village.

SUPPLY_RESERVE_DAYS: float = 7.0        # villages cannot resupply — reserve a round trip

# The remote check is a SNAPSHOT, and the numbers move under the mission in two ways
# (user 2026-08-20):
#   1. the village re-rolls its quantities every ~6 hours, so a check taken before a long
#      gather is already a rough estimate by the time the fleet arrives;
#   2. **bartering itself moves amity, up OR down, and crossing an amity TIER changes the
#      ratio** — higher amity is more favourable.  This is the common case, not the rare
#      one: 2 rounds at Melanesian took amity 60,000 → 92,760, Neutral → Trusting, i.e. the
#      tier changed *during* the mission.
# Both directions hurt a plan sized to the exact snapshot:
#   • tier UP   → more output per round → the hold overflows what was reserved for it;
#   • tier DOWN → more material per round → the fleet is short and loses a round.
# One cushion covers both, because the peak-hold bound scales the same way on each side:
# plan for (1 + cushion) × peak and buy (1 + cushion) × materials.  Sizing: observed
# per-tier output steps are ~5-10% (Camas 709 Neutral → 744 Favorable → 813 Friendly), so
# 15% absorbs a single tier change with room to spare.
AMITY_CUSHION: float = 0.15


@dataclass
class BarterRoundsPlan:
    rounds: int
    output_qty: int
    needs_per_round: dict = field(default_factory=dict)
    total_needs: dict = field(default_factory=dict)      # {material: units to OWN for all rounds}
    buy_targets: dict = field(default_factory=dict)      # {material: units still to buy}
    free_space: int = 0
    peak_per_round: int = 0             # nominal max(Σ needs, output) per round
    reserved_per_round: int = 0         # cushioned peak the rounds were actually sized on
    cushion: float = 0.0
    limited_by: str = "none"            # 'rounds' | 'space' | 'none'


def free_space_for_barter(cargo_capacity: int, cargo_used: int,
                          supply_reserve_days: float = SUPPLY_RESERVE_DAYS) -> int:
    """Hold space a barter mission may fill = capacity − cargo − a supply reserve.

    The reserve is `supply_needed_each(days)` for EACH of water and food (×2), per
    brain.supply_planner.  Assumes `cargo_used` counts trade goods only; if the HUD
    reading already includes supplies the reserve double-counts, which under-plans
    (safe) rather than over-plans (a stranded or overflowing fleet)."""
    from brain.supply_planner import supply_needed_each
    reserve = supply_needed_each(supply_reserve_days) * 2
    return int(max(0, cargo_capacity - cargo_used - reserve))


def plan_barter_rounds(output_per_round: int,
                       needs: Mapping[str, int],
                       rounds_remaining: int,
                       free_space: int,
                       materials_on_hand: Optional[Mapping[str, int]] = None,
                       cushion: float = AMITY_CUSHION) -> BarterRoundsPlan:
    """How many rounds to gather for, and what to buy — from the LIVE village check.

    `output_per_round` / `needs` come from the remote check (they refresh every ~6h, so
    the KB snapshot must not be used here); `rounds_remaining` is the Base tab's
    `barters_total − barters_used`.

    `cushion` hedges the snapshot going stale under the mission — a 6-hour re-roll or an
    amity tier crossing mid-barter (see AMITY_CUSHION).  It buys `(1+cushion)` × the
    materials and reserves `(1+cushion)` × the hold, so it can cost a round when the hold
    is nearly full; pass 0.0 to plan on the snapshot exactly.

    `output_qty` is what the fleet CARRIES AWAY, which past the point where the hold
    saturates is less than `rounds x output_per_round` — the game hands back only what fits.
    It is still built from the nominal snapshot, not the cushioned one: the cushion is space
    and material held in reserve, not extra output being predicted.

    Two quantity views come back: `total_needs` = what the hold must OWN for all rounds
    (what `buy_to_goal` wants — it does its own owned pre-check at the market), and
    `buy_targets` = that minus `materials_on_hand` for callers that already know it."""
    on_hand = dict(materials_on_hand or {})
    cushion = max(0.0, float(cushion))
    per_round_in = sum(int(q) for q in needs.values())
    peak = max(per_round_in, int(output_per_round))
    reserved = math.ceil(peak * (1.0 + cushion))

    # A round whose INPUTS ARE ALREADY ABOARD costs only the hold's NET growth, not the peak.
    # The village hands back the product as it takes the materials, so a round that consumes
    # 608 units and returns 679 grows the hold by 71 — the 608 are not needed a second time.
    # Live 2026-08-22 at Kolkata a fleet carrying FOUR rounds' materials planned ZERO rounds:
    # `free_space` (655) excluded the materials it had just gathered, yet `reserved` (781)
    # priced every round as if they still had to fit alongside the product. The mission had
    # trimmed its own cargo to hold those materials and was then deadlocked by them.
    held = {str(m).lower(): int(q) for m, q in on_hand.items()}
    funded = min((held.get(str(m).lower(), 0) // int(q)
                  for m, q in needs.items() if int(q) > 0), default=0)

    # A FUNDED ROUND COSTS NO SPACE AT ALL, because it is a SWAP and the game caps what it
    # hands back (user, 2026-09-04). The materials are already aboard; the round takes them
    # out and puts product in, and when the product does not fit the game offers to discard
    # the surplus — the "N has not been claimed yet, unclaimed trade goods will be discarded"
    # prompt. A full hold is therefore not a reason to refuse the round.
    #
    # THE VALUE RATIO IS WHY. Bambara Groundnut sold at 40,600 profit/unit against Pig and
    # Raisin at a few hundred; one round turns ~434 units of ~300-ducat material into up to
    # 1,036 units of 40,600-ducat product, about 300:1. At a full hold the round is a straight
    # UPGRADE of what is already there — 434 cheap units out, 434 expensive units in — and the
    # discard falls only on surplus that was never carryable.
    #
    # Live 2026-09-04 at Hutu the mission stopped holding 501 Pig and 1 Raisin. Converting
    # that dead weight was worth roughly 30M ducats: one more round would have taken 434 units
    # of material out and brought ~741 groundnut in before the hold filled.
    #
    # This was `max(0, output - per_round_in) * (1 + cushion)` — the NET growth, 692 here.
    # That is the right number for "how much does the hold grow", and the wrong one for "may
    # this round happen": growth that does not fit is discarded, not refused.
    #
    # UNFUNDED rounds still price the peak below. Their materials have to be BOUGHT, and space
    # for a purchase is a real constraint — you cannot discard your way into carrying it.
    reserved_funded = 0

    allowed = int(max(0, rounds_remaining))

    # SIMULATE THE ROUNDS INSTEAD OF PRICING THEM (user, 2026-09-04: "buying say 200 raisins
    # can do another round, and the exchange will swap the pigs and raisin to groundnuts, so
    # it will need to abandon some groundnuts, but still will get more profits").
    #
    # The old loop subtracted a per-round reservation from `free_space` until it ran out. That
    # cannot find the answer, because it prices a round BEFORE accounting for the materials
    # leaving the hold — and because it treats "the output does not all fit" as a REFUSAL when
    # the game's own answer is to hand back what fits and discard the rest.
    #
    # Measured on the 2026-09-04 Hutu run (free 4,568, 434 in, 1,036 out per round):
    #
    #     3 rounds  buy 1,302 material  ->  3,108 kept    <- what the old loop planned
    #     4 rounds  buy 1,736 material  ->  4,144 kept    <- what the game actually allowed
    #     5 rounds  buy 2,170 material  ->  4,568 kept    <- the hold saturates here
    #    10 rounds  buy 4,340 material  ->  4,568 kept    <- nothing further is gained
    #
    # At 40,600 profit/unit that is ~59M ducats between the plan and the optimum. Past
    # saturation more rounds only burn daily counts, gems and material for no cargo, so the
    # smallest round count that reaches the best yield is the one to take.
    def _kept(n: int) -> Optional[int]:
        """Units actually carried away after `n` rounds, or None if the materials cannot be
        loaded. Funded rounds' materials are already aboard and so are not in `free_space`."""
        space = int(free_space) - max(0, n - funded) * per_round_in
        if space < 0:
            return None
        kept = 0
        for _ in range(n):
            space += per_round_in                    # the round consumes them, freeing space
            got = min(int(output_per_round), space)  # the game hands back only what fits
            space -= got
            kept += got
        return kept

    rounds, best = 0, 0
    for n in range(1, allowed + 1):
        k = _kept(n)
        if k is None:
            break                                    # no room even to load the materials
        if k > best:
            best, rounds = k, n                      # strictly better, so take it
    limited_by = "space" if rounds < allowed else "rounds"

    total_needs = {m: math.ceil(int(q) * rounds * (1.0 + cushion)) for m, q in needs.items()}
    buy_targets = {m: max(0, q - int(on_hand.get(m, 0))) for m, q in total_needs.items()}
    # `best` is the SIMULATED yield, not `rounds x output_per_round`. Those parted company
    # when rounds stopped being sized so the whole output fits: at Hutu the nominal product of
    # 5 x 1,036 = 5,180 overstates by 612 units the hold cannot hold and the game discards.
    return BarterRoundsPlan(rounds=rounds, output_qty=best,
                            needs_per_round=dict(needs), total_needs=total_needs,
                            buy_targets=buy_targets,
                            free_space=int(free_space), peak_per_round=peak,
                            reserved_per_round=reserved, cushion=cushion,
                            limited_by=limited_by)


# ── On-arrival ground truth (the village barter panel) ────────────────────────
# The plan built before sailing is an estimate; the panel in front of the fleet is fact.
# `X/Y` per material is fleet-has / per-round requirement as displayed, so the rounds the
# hold can actually fund is `solve_barter_quantity`'s material bound over the panel rows —
# no new bound, just the canonical one fed with live numbers.
#
# This is what catches cargo that did not survive the voyage: the 2026-08-20 fleet death
# took 75% of the materials and the shortfall was only inferred AFTERWARDS, because the
# barter loop discarded the panel read it had already taken.

@dataclass
class PanelBarterState:
    rounds_remaining: int                       # FULL rounds the materials aboard can fund
    partial_fraction: float = 0.0               # fraction of ONE more round fundable (0-1)
    binding: Optional[str] = None               # the material that runs out first
    materials: dict = field(default_factory=dict)   # {label: (have, need)}
    shortfall: dict = field(default_factory=dict)   # {label: units short of one more round}


def _have_need(m) -> Tuple[int, int]:
    """Accept a BarterMaterial (.have/.need) or a plain (have, need) pair."""
    if isinstance(m, (tuple, list)):
        return int(m[0]), int(m[1])
    return int(getattr(m, "have", 0)), int(getattr(m, "need", 0))


def panel_barter_state(materials, output_per_round: int = 0,
                       rounds_cap: int = 99) -> PanelBarterState:
    """Rounds the fleet can actually fund, read off the live barter panel.

    `materials` are the panel's rows (`actions.barter_reader.BarterMaterial`, or
    `(have, need)` pairs).  Rows are keyed by POSITION, not label — the panel's labels are
    tile OCR and can repeat or come back as a category.

    Also reports the leftover as `partial_fraction`.  The panel carries a QUANTITY STEPPER
    (1-200, default max) and the X/Y shown is AT THE CURRENT STEPPER VALUE, so a reduced
    exchange is a full, valid one that costs a daily barter count exactly like a max-size
    one (docs/barter_apache_walkthrough_notes.md, frames 2/5; Melanesian round 2 ran at
    ~9.9% and yielded 53 of 552).

    DECIDED (user 2026-08-20): **always barter at FULL size** — the stepper is rarely used.
    So `rounds_remaining` counts MAX-SIZE exchanges only, leftovers that cannot fund one are
    simply not bartered, and nothing reads or sets the stepper.  `partial_fraction` is kept
    as REPORTING (it explains a shortfall) — do not turn it into a commit."""
    rows, labels, on_hand, mats = [], {}, {}, {}
    for i, m in enumerate(materials or []):
        have, need = _have_need(m)
        key = f"{i}"
        label = (getattr(m, "label", "") or "").strip() or f"#{i}"
        rows.append((key, need))
        labels[key] = label
        on_hand[key] = have
        mats[label] = (have, need)
    if not rows:
        return PanelBarterState(rounds_remaining=0)

    plan = solve_barter_quantity(int(output_per_round), rows, on_hand,
                                 rounds_remaining=rounds_cap, free_space=0)
    rounds = plan.rounds

    left = {k: on_hand[k] - rounds * need for k, need in rows}
    fundable = [(left[k] / need, k) for k, need in rows if need > 0]
    partial, binding_key = min(fundable) if fundable else (0.0, None)
    shortfall = {labels[k]: need - left[k] for k, need in rows if left[k] < need}
    return PanelBarterState(rounds_remaining=rounds,
                            partial_fraction=round(max(0.0, partial), 4),
                            binding=labels.get(binding_key), materials=mats,
                            shortfall=shortfall)
