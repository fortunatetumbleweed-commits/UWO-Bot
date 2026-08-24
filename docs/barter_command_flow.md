# The Barter Command Flow — review & build plan (2026-08-20)

Status: **built 2026-08-20 (steps 1–4), not yet run live**. This is the durable copy of the in-session
barter-flow review (gap analysis) + the target design. Companion memory:
`project_remote_village_barter_check_2026-08-20` (remote-check mechanics),
`project_village_find_worldmap_2026-08-20` (village nav + first-mission lessons).
**Richest mechanics source: `docs/barter_apache_walkthrough_notes.md`** (human-played
Camas run — the stepper, negotiation, the amity ladder, the overflow-discard flow, remote
sell scouting). Read it before extending anything here.

## Target UX

One typed command drives the whole mission:

```
barter Box of Nutmeg at Melanesian Village, then take the route jakarta to london
barter Camas at Apache Village, and sail to Edinburgh
```

Grammar: `barter <good> at <village>[, then take the route <route-name> | and sail to <port>]`
— `<route-name>` is a pre-planned in-game route (Route tab); "sail to" is a free-sail destination.

## Target flow

```
parse → CHECK village (remote, from port) → PLAN (capacity/supply-aware) → GATHER
      → sail to village → BARTER (all available rounds) → ROUTE/SAIL to destination
      → SELL (+ trade-point award)
```

### The recipe model (load-bearing)
A barter good's **materials are invariant**, but the **quantities refresh every ~6 hours**
(the countdown timer on the village screen): need-per-material, obtain-per-round, and the
daily rounds (`Daily Barter Progress N/7`). Therefore the bot **constructs the live recipe
remotely before setting sail** — that's the planning input, not the KB snapshot.
`recipes.json` keeps only invariants (good ↔ materials, villages, source ports, preconditions,
season); volatiles come from the check. Live proof of drift: KB said 136/204/180 → 591;
the live panel said 152/180/204 → 552.

Amity is the second driver: bartering moves amity (up or down), and **crossing an amity TIER
changes the ratio — higher amity is more favourable** (Camas 709 Neutral → 744 Favorable → 813
Friendly, ~5-10% a tier). A tier crossing mid-mission is normal, not rare: 2 rounds at Melanesian
moved amity 60,000 → 92,760, Neutral → Trusting. Hence `AMITY_CUSHION` (see Flags).

### The barter panel's numbers (settled 2026-08-20)
`X/Y` per material = **fleet-has / per-round requirement, exactly as displayed** — no hidden
multiplier, no per-ship division. Exchange greys out when any material is short.

An Exchange can go through at a REDUCED SIZE via the quantity stepper (1–200), and everything
scales with it — output, material cost, and amity. Melanesian round 2: materials left after
round 1 funded ~9.9% of a max exchange → **53** units observed against ~54 predicted, and the
amity delta scaled identically (+2,901 after +29,859 = 9.7%). Two independent quantities
agreeing to <1%. (At the time this looked like a mysterious "partial"; the walkthrough's
stepper frames supply the mechanism.)

⚠️ Do not re-derive a "4× consumption" mechanic from that session. The panel's X looked ~4×
short of what the bot believed it carried because **75% of the cargo had been lost to the fleet
death** — Ebony 681→167, Coral 1020→252, Textiles 900→222, all three at exactly 25%. It was a
cargo-verification failure, not a barter rule. The fix is reading the panel on arrival (#6),
which is now wired.

## Gap analysis (review of 2026-08-20)

| # | Step | Status | Notes |
|---|---|---|---|
| 1 | Command parsing | ✅ built | `brain/barter_command.parse_barter_command` + `run_barter_command`; CLI `run_barter.py "<command>" [--dry-run] [--capacity=N --cargo=N]`. `then`/`and` interchangeable, comma optional. |
| 2 | Village pre-check (remote) | ✅ built | `actions/village_check.read_village_barter_remote(village, good=…)` — open world map → `pan_to_village` → tap → verify the panel → Base tab → Barter tab → scroll-accumulate (stops on a repeated `screen_signature`) → **taps the location pin of each material whose source is still unknown** (`material_pins` + the existing `read_material_sources`) → `write_back_invariants` → `exit_to_overworld`. Never taps *Move to Village*. |
| 3 | Capacity/supply plan | ✅ built | `brain/barter_quantity.free_space_for_barter` + `plan_barter_rounds` (fed by the check, never the KB). **Correction to the original wiring**: the pre-gather bound is the PEAK hold usage, `max(Σ needs, output)` per round — not `solve_barter_quantity`'s net fill. Net fill answers "how many rounds can I commit with materials already aboard" (still the right call at the village); it would authorise a gather too large to *carry* to the village. Returns `total_needs` (the own-N goal `buy_to_goal` wants) and `buy_targets` (minus on-hand). |
| 4 | Gather | ✅ built | `gathering_solver` (source-port set-cover + route) + `buy_to_goal` (owned pre-check, cargo-full guard — both live-validated). Surplus clearing is `sell_goods(goal="clear", keep=barter_materials_exclude(good))`, which already existed and was used live before the Nutmeg run; `clear_surplus_at_current_port` now wraps it and `run_barter_command(clear_surplus=True)` / `--clear-surplus` runs it **before** the cargo read, so the plan sizes against space that is actually free. Opt-in: a clear-sell ignores profit and can dump cargo meant for a better market. |
| 5 | Sail to village | ✅ validated live | Anchor-port typed-search + Explore-tab label recognition (all four 2026-08-20 nav fixes). |
| 6 | Barter rounds | ✅ built (panel-driven); overflow-discard UNWIRED | `barter_commit_verified` + `run_barter_phase`, now sized by the PANEL: `_read_panel_state()` → `brain.barter_quantity.panel_barter_state` runs the canonical `solve_barter_quantity` over the panel's X/Y rows. The arrival read **bounds the round target** (a per-round gate alone keeps attempting rounds on a stale read) and a shortfall vs the plan is logged loud + returned as `material_shortfall`. Partial rounds are reported, not spent. Unreadable panel → permissive fallback, since the panel may not be open yet and every commit is verified anyway. Overflow is now WIRED: `read_state` reports pending units from the dialog and `jettison_fn` runs `actions/overflow_dialog.clear_overflow` — probe each cargo tile to learn what it is (the tiles show only a quantity; the Discard dialog names the item and always offers Cancel), plan with `jettison_planner.plan_jettison`, discard exact quantities, then Receive. Simulated against the recorded Camas overflow (see below). |
| 7 | Route leg | ✅ wired | `MissionTail(kind='route')` → the `sail_route` executor: `execute_route(name, longest_leg_days=6)` then `_await_route_arrival` (sleeps a supply-derived interval, jittered, until a port is read; structured failure on timeout). A route ends where it ends, so `sell_port` is **resolved on arrival** from `where_am_i`, not planned. `kind='sail'` keeps the free-sail leg. |
| 8 | Sell + award | ✅ done | Goal-driven `sell_goods` (profit/clear) + `get_trade_point_award` (auto after sell). |
| 9 | Sell-down-to-N | ✅ built | `actions.sell_goods.sell_down_to(port, keep={good: units_to_keep})` — bulk OFF → tile → qty dialog → `market_actions.type_quantity_on_keypad` (**reads the typed value back and only presses ↵ once it matches** — the recorded '21 for 218' drop would otherwise dump the wrong cargo) → Load → one Sell for all trims. **Safety model: loading is reversible, committing is not** — a missing qty dialog (Put In Bulk still ON ⇒ the tap loaded the WHOLE stack), an unconfirmed quantity, a dialog still open after Load, or a missing Sell button all abort with nothing sold. Every exit restores Put In Bulk ON, because the BUY flow silently breaks with it OFF. |
| 10 | Supply discipline | ✅ wired (village + route legs, dynamic watch) | 7-day space reserved in the plan (#3). **The gate lives AT SEA, not in port** (user 2026-08-21): supplies load as part of Supply Departure, so 0 supply in port is normal and gating on it would abort a good mission. `supply_verify` therefore only confirms the fleet is somewhere it CAN supply (a port, or already at sea where it does gate on days); the village leg passes a round-trip FLOOR (`VILLAGE_LEG_RESERVE_DAYS`) into the at-sea watch, which enforces it the whole way and diverts to resupply if it drops short — a village has no harbour, so the one-way `eta + 2` was never enough. Route tail asserts ≥ 6 days (user: routes are planned so no leg exceeds 6). Both sail loops now re-check supply on a **supply-derived cadence** (~1 game day before empty), not a fixed clock. |
| 11 | Orchestration | ✅ reworked (+ pre-village trim) | `build_barter_graph(opp, plan, tail=MissionTail(…))` = gathers → **sell_surplus** (trim each material to `plan.needs`) → **supply_verify** → sail_to_village → barter → **route \| sail_to_sell \| nothing** → sell. The CHECK is deliberately **not** a node — every downstream node is parameterised by what it returns, so it runs before the graph is built (`run_barter_command`), and the opt-in surplus clear runs there too for the same reason: it changes the free space the plan is sized against. |

## Flags
- **Recipe coverage**: `recipes.json` has only `box_of_nutmeg`. Design resolves this: the remote
  check IS the recipe source (invariants written back on first read), so any village works with
  zero manual KB entry. Source ports can be learned from the material rows' location pins.
- **The exchange QUANTITY STEPPER (1–200) is the mechanism** — settled by
  `docs/barter_apache_walkthrough_notes.md` (frames 2, 5). Output and material needs both
  SCALE with the stepper, and the panel's `X/Y` is read AT THE CURRENT STEPPER VALUE; the
  default appears to be max. Stepper=1 exposes the base ratio (Pulque 3 ← 1 Coral + 1 Silver).
  Consequence for the code: `panel_barter_state`'s "partial fraction" is not a degraded round —
  it is a **full, valid exchange at a lower stepper setting**, costing one daily barter count
  like any other. **DECIDED (user 2026-08-20): always barter at FULL size; the stepper is rarely
  used and stays unread/unset.** Leftovers that cannot fund a max exchange are not bartered;
  `partial_fraction` stays as reporting only (it explains a shortfall), never a commit.
- **The check is a SNAPSHOT — the ratio moves under the mission** (user 2026-08-20). Two
  drivers: the ~6-hour re-roll, and **amity**. Bartering itself moves amity (up *or* down), and
  **crossing an amity TIER changes the ratio — higher amity is more favourable.** This is the
  common case, not the rare one: 2 rounds at Melanesian took amity 60,000 → 92,760,
  Neutral → Trusting, i.e. the tier changed mid-mission. Both directions break a plan sized to
  the snapshot exactly — tier UP yields more output than the hold reserved for it, tier DOWN
  demands more material than was bought. `AMITY_CUSHION = 0.15` covers both with one knob
  (`plan_barter_rounds(cushion=…)`, `run_barter.py --cushion=`): reserve `(1+c) × peak` of the
  hold and buy `(1+c) ×` the materials. Sized from the observed per-tier output steps of ~5-10%
  (Camas 709 Neutral → 744 Favorable → 813 Friendly). It can cost a round when the hold is
  nearly full — `--cushion=0` plans on the snapshot exactly. **The cushion is a pre-sail hedge,
  not a substitute for gap #6**: the on-arrival panel is still the only ground truth.

## Live-time model (user 2026-08-20)

**A game day is under 2 real minutes — ~1.5** (`GAME_DAY_SECONDS = 90`), corroborated by the
independent observation already in `actions/route_execution.py` ("~2 real-min per game-day;
12-day route ≈ 25 min"). We take the short end so every derived wait fires early, which is the
safe direction.

The monitor's cadence therefore comes from the days on the HUD, not a fixed interval:
`supply_checkback_seconds(days_left)` waits out the banked supply **minus a 1-day margin**, so
the re-read lands while supply is still in hand — 5 days → 6 real minutes, the user's reference
case. (An earlier draft added the margin instead of subtracting it, which would have re-read
after the tank was already dry.) Water and food burn at the same rate, so one reading of
amount-held ÷ days-shown calibrates per-day consumption (`per_day_from_reading`).

The mid-voyage watch is now **dynamic** (user 2026-08-20): once under way, read the HUD, then
schedule the next read for about **one game day before the tank runs dry** —
`task_runner._next_supply_check_s(days)` → `supply_checkback_seconds`. Both sail loops use it
(`run_sail_to` and `drive_sail_to`). Unreadable supply is never read as "plenty": it falls back
to a short fixed wait (`_SUPPLY_CHECK_FALLBACK_S`, 120s). The old fixed 180s poll is gone — it
was ~2 game days between looks against a 2-day divert buffer, so one missed window could eat the
whole buffer, while a full tank was re-OCR'd for nothing.

## Future: natural-language commands via Qwen

The grammar today is a regex (`barter <good> at <village>[, then take the route <name> |
and sail to <port>]`) — it parses exactly what it was written for and rejects everything
else. The intent is for **Qwen, given the game knowledge, to take the command in free
natural language** and emit the same `BarterCommand` structure, so the phrasing stops
mattering. Deferred (user 2026-08-21); the regex stays the fallback and the structured
output is already the seam a model would fill.

## Build order
1. **`read_village_barter_remote(village)`** — package the validated parsers with the
   navigate-in flow + KB invariant write-back.
2. **Command parser + driver** (`barter <good> at <village> …`).
3. **Plan node** — wire `solve_barter_quantity` with capacity/supply/rounds as above.
4. **Graph rework** — check → gather(+surplus-sell) → supply-verify → village → barter
   (panel-driven rounds) → route/sail-to → sell+award.
5. **Sell-down-to-N API** ✅ + supply-monitor wiring ✅ (dynamic cadence; the village-leg
   ROUND-TRIP requirement is still open — `_ensure_supply` gates on one-way `eta + 2`).

Steps 2–4 make the command run end-to-end (with the check); 5 completes surplus + safety.

**Built 2026-08-20 — steps 1–4 (unit-tested, NOT yet run live).**
`run_barter.py "barter <good> at <village>[, then take the route <n> | and sail to <p>]"`.
New/changed: `actions/village_check.py` (driver + `material_pins`/`screen_signature`/
`write_back_invariants`), `actions/fleet_status.py` (new — main-menu supply/cargo read),
`brain/barter_command.py` (new), `brain/barter_quantity.py` (+plan), `brain/supply_planner.py`
(+live cadence), `brain/mission.py` (`MissionTail`, supply_verify), `brain/barter_mission_live.py`
(`supply_verify`/`sail_route` executors, arrival-resolved sell port).
### Overflow flow — simulated against the recorded Camas run (2026-08-21)
Replayed from `data/sessions/barter_apache_walkthrough_2026-08-14T12-48-30` frames 14-21
(pending 143 → 131 → 100 → 0; Camas 3,613 → 3,756 = exactly +143; supplies 226 → 176).
The readers parse every frame of that sequence, and the simulation caught a real defect:
**pricing the barter output high is not protection.** `plan_jettison` sorts by value but
still walks every trade good before touching a supply, so it planned to dump 100 Camas
while 122 units of spare supply sat untouched. The output is now EXCLUDED from the
candidate list, after which the plan matches what the human did — Cassava 31 + Avocado 12,
then spare supply only. Under the stricter 7-day village reserve it reports
`shortfall=32` rather than cutting into supply: the caller then knowingly sacrifices 32
units of output instead of starving the fleet.

Still open:
the village-leg round-trip supply monitor (`task_runner._ensure_supply` gates on
one-way `eta + 2`, but a village has no harbour), and the
`actions/village_remote_reader.read_barter_ratios` prototype now superseded by
`village_check.parse_trade_list` (two readers for one concern — consolidate).
