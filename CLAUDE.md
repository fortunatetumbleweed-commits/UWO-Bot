# UWO Bot — Claude Code Context

## Guiding Principles — read first; everything in Active Rules follows from these

**0. WE DO NOT CONTROL THE GAME. WE OBSERVE IT.**
Android's framework AUTHORS the screen, so it can hold the truth: `getRunningTasks()` is a
lookup, not a guess, and an event cannot fail to find a window. We only ever look at what
the game has already done — and it can put an unexpected screen, dialog or popup in front of
us at any moment, without telling us. Every rule below is a consequence of that one
asymmetry. When a rule seems arbitrary, re-derive it from here.

**1. Centralize the observation and the routing; localize the interpretation.**
One perceive, one dispatcher. The dispatcher decides WHO handles a screen; it never decides
what the screen MEANS. Meaning belongs to the activity, inside its own context — a dialog,
a tab switch, a barter round change the screen without changing the world, and none of them
is a transition. Borrowed from Android's split between intent dispatch (coarse, rare,
central) and input dispatch (fine-grained, constant, centrally ROUTED but locally
INTERPRETED). See `docs/activity_as_context.md`.

**2. Never store a conclusion — store the observation, recompute the conclusion.**
An observation can be checked by looking; a conclusion outlives the evidence for it. The
test: could a fresh screenshot prove this false? If it would take reasoning or history, do
not write it down.

**3. DO NOT ASSUME — enumerate the causes, then read the one that tells them apart.**
Distinct from #2, and worth keeping separate: a stored conclusion was RIGHT ONCE and went
stale; an assumption was NEVER CHECKED. It is a branch that treats one possible explanation
as THE explanation, hardcoded when the code was written, and it is wrong from that moment —
it just waits for the unconsidered case to arrive.

Live 2026-08-27, the crash: *"the Exchange tap changed nothing, so the panel must be
stale."* A dead Exchange has THREE causes — a material at 0, the day's rounds spent (the
game CLOSES the submenu), or a genuinely stale panel — and the code enumerated one. It took
the stale branch into a refresh path that had never once executed and ended the run.

The same shape, the same day: *"a panel opened, so it is the port list"* (every list has a
search box); *"the lit tab is the warm one"* (the world map lights WHITE, so warmth ranked
the selected tab LOWEST); *"OmniParser returns clean labels"* (it returned `NarExplore`);
*"one number can answer a per-good question"* (Iron's surplus masked Matchlock's shortfall).

The test: **how many ways could this observation have come about?** If more than one, read
the thing that distinguishes them — it is nearly always on the screen already. A grey
Exchange and a closed submenu are different pixels, not different inferences.

**4. Data has an OWNER and dies with it.** `COMPANY > FLEET > PLACE > BUILDING > PANEL`.
Ask not "where did I read this?" but "who does it belong to?" — only the second predicts
when it goes bad. Two ways it does: LEAVING and ACTING.

**5. One loop — the task drives the state machine. No flow sub-loops.**
A loop may wait for its OWN effect; it may never perform a sequence. Nothing delivers us an
`onPause`: a loop that does not return cannot be told the world moved under it, and every
one we have written has grown a private, worse copy of the dispatcher to compensate.

**6. Check before acting, not after.** A precondition verified by spending the taps that
were supposed to test it is not a precondition. Confirm the tab before reading the rail,
the screen before tapping a calibrated point, the state before dispatching an intent.

**7. The task runner is PASSIVE — it is consulted, it does not drive.**
When the dispatcher settles to a world it *consults* the task manager for a work order, and
the dispatcher dispatches it. The task runner does exactly two things: **provide the work
order, and update task status.** It calls nothing.

  - It does not **call down**. It never perceives — it is *handed* `state`. Going to look for
    itself means reading a world the dispatcher owns, which is how a conclusion gets stored
    (#0, #1, #2).
  - It does not **call up**. It never invokes the goal loop. If it can call the loop that
    calls it, it is the driver again under another name (#5).

Its whole signature is the slot the dispatcher already declares: `next_goal(result, state)`.
A task-layer module that needs to *look* does not import a reader — it returns the work order
that asks an activity to look, and reads the answer when it is consulted again.
Enforced by `tests/test_the_layering_is_enforced.py`; the layers are declared in
`brain/layers.py`.


## Project Overview
A Python bot that plays **Uncharted Waters Origin (UWO)** autonomously
on an Android phone, mirrored to Mac via scrcpy.  Goal: grow the
in-game company across **trade**, **exploration**, and **combat** — all
three in parallel.  Navigation supports them, not the other way around.
Long-term goal is self-learning: earn ducats → buy better gear / ships
→ unlock new routes → discover more → earn more.

## Knowledge Map — where to find / put things
The project's knowledge lives in four places, each with different
freshness guarantees:

| Where | What | Authoritative? | Updates how |
|---|---|---|---|
| **Code + tests** | Bot's actual behaviour | ✓ ground truth | Every commit |
| **`memory/`** (`~/.claude/projects/.../memory/`) — index in `MEMORY.md` | Per-incident lessons, user preferences, gotchas | Notes about truth | Auto, when I learn something new or you correct me |
| **`CLAUDE.md`** (this file) | Concise project-level rules, current milestone, knowledge map | Project intent | Manual; **must be updated** when major decisions or phase shifts happen — see `memory/feedback_claudemd_update_on_decisions.md` |
| **`docs/`** | Long-form designs, schemas, milestone detail, game mechanics | Authoritative when current; can drift | Manual; updated alongside code |

When in doubt about where to put something new:
- A behaviour rule that applies to every future change → memory `feedback_*` entry
- A non-obvious project fact (e.g. game quirk) → memory `project_*` entry
- A pointer to an external system (Linear, Slack, dashboard, etc.) → memory `reference_*` entry
- A design decision that affects how features should be built → CLAUDE.md (here) + a doc in `docs/`
- A long explanation, diagram, table, or schema → `docs/`

## Trace Viewer Reports
Every live run writes an action trace to `data/sessions/trace_<name>_<ts>/`.  To review it:

```bash
python -m tools.trace_viewer data/sessions/trace_<name>_<ts>/
open -a Safari "file://$PWD/data/sessions/trace_<name>_<ts>/viewer.html"
```

**Always open the report in Safari as part of generating it** — not as a separate step to be
asked for.  The report exists to be looked at frame by frame; left as a path on disk the
review does not happen.  When summarising, name the frames that matter ("frames 8-12 are the
repeated taps at the hardcoded close-X"), because the review navigates to specific ticks.

`--qwen` adds a Qwen read per frame (slow); `--rebuild` ignores cached component data.
Generation reuses perception the bot already recorded where available; see
`actions/action_trace.py::_capture_with_perception`.

## Run Analysis — after EVERY run
```bash
python -m tools.analyze_run                 # today's log
python -m tools.analyze_run --since 10:30   # one run's window
```
Counts what failed and how often, and flags **checks that almost always fail**.  A single
failure is noise; the same failure eighty-five times is a defect, and the only thing between
them is arithmetic.

Why this is a standing step, not an optional one: on 2026-08-26 `title_says_world_map`
returned False on **85 of 89** looks from the day's first departure.  Every gathering leg
still sailed — each needed only ONE lucky read and the goal retried until it got one — so the
defect cost nothing until the Lisboa leg, and had been visible in the log for ten hours.  Run
over that morning alone, this report surfaces it, the port-name reader and the bulk checkbox:
all three bugs that were then found one at a time over the following hours.

Read the SUSPECT lines first.  A flag that is always False may simply not apply
(`has_sea_hud` in port); one that is False 93% of the time, having been True three times, is
a check that works and usually does not — which is what a broken read looks like from outside.

## Voyage Evaluation Procedure
Standard, single-command voyage evaluation against the canonical
Cairo→Y-tip reference path:

```bash
python -m tools.evaluate_voyage data/sessions/explore_port_<ts>/
```

The procedure:
1. Loads the standard reference from `data/reference/standard_nile.json`
   (canonical session, outbound clip, tolerance, lookahead).
2. Runs `tools/compare_paths` to identify deviation segments vs
   the reference path.
3. Drills into each segment's tick data to surface *major issue
   ticks* — rejection bursts, OCR jumps, picker fires, §13.21 commit
   resets — and infers the likely root cause.
4. Emits VERDICT (PASS / WARN / FAIL) + per-segment narrative +
   ranked issue-tick list to `voyage_eval.md` and `voyage_eval.json`
   in the session dir.
5. Exit code reflects the verdict so this can gate CI / scripts.

Use this every voyage.  Jump straight to the surfaced issue ticks in
`tick_viewer.py` (press `g`, type the tick number, Enter) to inspect
the frame + trace context.

## Active Rules
*The detail behind the Guiding Principles above. Where a rule here and a principle there
appear to disagree, the principle wins and the rule is out of date.*

- **Never store a conclusion — store the observation, recompute the conclusion** —
  an **observation** is something that can be checked by looking at the screen
  (`state='sea'`, `Exchange is grey`, `shelf shows 0`).  A **conclusion** is what you
  inferred from one (`the departure failed`, `the village is out of rounds`, `the good
  is unavailable`).  Observations may be cached — scoped to their world, see below.
  Conclusions may **not**: recompute them from a fresh look, every time.
  - **Why**: a conclusion outlives the evidence for it, and it is usually drawn from an
    observation that had a second explanation nobody considered.  Live 2026-08-26:
    `commit_departure` concluded "could not depart" from "no destination panel on the
    map" — true after a FAILED departure, and equally true after a SUCCESSFUL one,
    because the panel closes either way.  The fleet was already at sea.  The goal held
    the conclusion, re-opened the world map **mid-voyage**, re-targeted the port it had
    just left, sailed back, bought nothing, and repeated.
  - **The test**: could a fresh screenshot prove this false?  If yes it is an
    observation.  If it would take reasoning, history, or "well, it should have
    worked", it is a conclusion — do not write it down.
  - **In practice**: a primitive that acts does **not** report whether it worked.  It
    taps and returns; the next perceive says what happened.  Any function whose result
    is a verdict on its own success is this bug waiting to happen — most of all when
    its success test asks a question that can only be answered *before* it succeeds.
  - Corollary of *a transition is a switch of activity, however long it takes*
    (`docs/architecture_DRAFT.md`).  Same rule, arrived at from the data side.
- **A transaction's RESULT DIALOG is the proof it happened — the count is a separate
  reading, and disagreement means the READING is wrong** (user, 2026-08-26).  After a
  buy/sell/commit the game shows a result dialog; `_react_after_purchase` already logs it
  as `[txn] dialog OK (confirm=False, result=True)`.  That is the observation that the
  transaction SUCCEEDED.
  - The owned/cargo count is a DIFFERENT observation, read from the right-hand panel, and
    it can fail on its own: with a cluttered hold the good's tile is **below the fold**, so
    the position tracker has no tile to follow.  There is no right-panel scroll in the
    codebase (`MARKET_SCROLL_START` at x=1500 is the CENTRE grid) — only the generic
    `actions.ui.scroll`.
  - So when the result dialog says success and the number did not move, the conclusion is
    **"I am tracking the wrong tile, or the tile is not showing"** — never "the buy
    failed", and never "I own 0".
  - **This has now cost three runs.** 2026-08-21: tracker returned None every round and the
    bot bought 1,681 Ebony against a goal of 350.  2026-08-26: `owned=UNREADABLE ... 0/470`
    for four straight rounds while ~1,300 Iron went aboard, ~558k ducats and 4 blue-gem
    refreshes.  The response the first time was to add a log line that made the blindness
    visible; the read was never fixed.
  - **COUNT WHAT THIS SESSION BOUGHT** (user, 2026-08-26).  The result dialog names the
    amount, so add it up.  A loop that cannot read the hold still knows perfectly well what
    it has put in the hold *since it started*, and that alone bounds it: "I have bought
    2,000 against a goal of 470" needs no cargo panel at all.  This is the fix that works
    when the tile is unreadable, which is exactly when it is needed.
  - **A CLEAR IS ONLY DONE WHEN THE SELL PAGE HOLDS NOTHING BUT MATERIALS** (user,
    2026-08-26) — checked across the WHOLE list, scrolled, not the visible page.  An empty
    visible page is not an empty hold.
    - **COUNT THE TILES — more tiles than (materials + Water + Food) means surplus remains**
      (user, 2026-08-26).  A cheap, whole-panel completeness test that needs NO scrolling to
      DETECT the problem, only to fix it: the trim knows exactly what it intended to keep, so
      any tile beyond that count is something it failed to sell.  Counting is an observation;
      "the visible page had nothing sellable" is a conclusion, and it is the one that ended
      tonight's clear with the hold still full.
    - The owned quantity per good is printed ON ITS TILE in the Sell grid — the very reading
      `_read_owned_via_sell` wants.  Live 2026-08-26 the grid showed `Iron 1,451` while
      `buy_to_goal` reported `0/470`: the number was on screen, one page down.
    - NOTE for the buy side: a SELL result dialog reports MONEY (Total Amount 9,461), not
      units.  Accumulating "what this session bought" needs the buy dialog's units, or a
      count of confirmed rounds — the per-good quantity lives on the tile, not in the dialog.
  - **Nothing scrolls a market list at the point of ACTING, and that is one bug, not
    three.** `read_market_all_pages` scrolls and accumulates; the sell loop and the
    owned-count read both call the single-page `read_market_page_omni`.  Live 2026-08-26 the
    surplus clear read `omni grid 3x3: 9 goods` four times, sold 36 goods, found nothing
    sellable left IN VIEW and quit with the hold still full below the fold — while the buy
    beside it reported `owned=UNREADABLE ... 0/470` and bought past 2,000.  Selling re-flows
    the grid, so the fix is sell-page → scroll → repeat, never read-all-then-tap.
  - Trimming the hold first (`--clear-surplus`) helps and is worth doing, but it is a
    mitigation: it shortens the list, it does not make the reader able to see past the fold.
- **Data has an OWNER, and dies with it** — ask not "where did I read this?" but
  **"who does it belong to?"**  Only the second predicts when it goes bad.
  - `COMPANY` — ducats, gems, the mission and its plan — **always valid**
  - `FLEET` — cargo, capacity, supply, crew, ship life — **valid in every world, it
    moves with you**
  - `PLACE` — this port/village: buildings, prices, amity, today's rounds — dies on leaving
  - `BUILDING` — what this market stocks, which tab is open — dies on leaving it
  - `PANEL` — the cart, the selected tile, the open dialog — dies when the panel closes
  - **The cart makes it concrete**: the market's right panel shows the cart AND the hold
    in one rectangle — active tiles staged, greyed tiles already owned
    (`memory/market-right-panel-is-cart-and-cargo`).  Same glance, two owners: leave the
    market and the **cart is gone** (a remembered cart is an instruction to buy things
    nobody chose), while the **cargo sails with the ship**.  So "where I read it" cannot
    be the key.
  - **Two ways data goes bad, not one**: **LEAVING** kills `PLACE`/`BUILDING`/`PANEL`
    data (automatic, on transition); **ACTING** kills whatever the action changed
    (buying changes the hold; bartering changes the hold, the amity and today's rounds).
    Fleet data is immune to the first and fully exposed to the second.
  - `PLACE` data is a cache to re-read cheaply, never a record to trust — a shelf
    restocks on a timer, so it goes stale with nobody moving at all.
  - Corollary: the mission is `COMPANY`-owned, so it must **never** hold anything from a
    shorter-lived owner.  A task runner that remembers which tab was open has taken
    custody of something that dies without telling it — which is the layering rule and
    the lifetime rule turning out to be the same rule.
  See `docs/architecture_DRAFT.md` → *Who OWNS the datum decides when it dies*.
- **Never act blind — always know WHERE and WHAT STATE, else re-perceive** —
  before taking any action the bot must know both (1) its **location**
  (port_overworld / market / building / sub_menu / sea / village / world_map …)
  and (2) its **interaction state** (mid-transaction with the bot's own dialogs,
  vs. blocked by an unsolicited popup like daily-news / promo / event).  If either
  is unknown it must **re-perceive** — never guess or tap.  If still unknown after
  re-perceiving, **escalate / learn**, don't flail.  Some facts are **invariants**:
  a `port_overworld` ALWAYS has a port name — failing to read one there is an
  anomaly to flag + retry (re-capture, stronger read), NOT a silent `None`.  This
  is the foundation for the action-verification/recovery ladder
  (`docs/action_verification_and_recovery_design.md`).
  - **Dialog / popup handling — who caused it decides how to clear it:**
    - **From the bot's OWN action** (a Confirm / Result / Negotiation after a
      buy / sell / commit / recruit): **complete the action** by clicking through
      its action buttons (Confirm / OK / Purchase / …), never dismiss it.
    - **System / unsolicited** (daily news, promo, event, announcement — NOT
      triggered by the bot): **dismiss** it — the Back button, or the dialog's own
      **close X** (an X-shaped icon in or near the **top-right** of the popup).
      Never the screen-corner (at sea/port_overworld that is the ☰ hamburger —
      see `memory/project_home_button_is_chromed_only_escape`).
- **Surgical changes** — touch only what the task requires.  Don't refactor
  adjacent code, don't add unrequested features, don't introduce
  abstractions for hypothetical use.
- **Scoped test runs** — run only the tests for modules you edited;
  escalate to the full suite when changes touch ≥3 subdirs or critical-
  path modules (perception, recovery, planner, FSM core).
- **Flow completeness** — a flow is complete only when it (1) ends at a
  recognised state AND (2) contains at least one positive transaction
  (confirm / buy / sell / recruit / set-sail).  Back / Home = Cancel,
  not progress.  See `docs/flow_and_plan_completeness.md`.
- **Plan completeness** — Claude-generated plans are recommendations
  bound by the same rule.  On step-miss, refine via
  `commit_via_positive_taps` or revamp via Claude.  Plans with zero
  transactions must NOT be persisted as learned recoveries.  See
  `docs/flow_and_plan_completeness.md`.
- **CLAUDE.md stays current** — when a major decision is made or a phase
  status changes, propose a CLAUDE.md edit in the same session.  See
  `memory/feedback_claudemd_update_on_decisions.md`.
- **DOWNSCALED IMAGES ARE FOR COARSE JUDGMENTS ONLY — never for CONTENT** (user,
  2026-08-26).  A reduced image answers *which screen is this*, *did a popup appear or go
  away*, *did a whole panel change* — questions about big regions.  It cannot answer *what
  does this say* or *how many*.  This game puts small text everywhere — quantities on
  thumbnails, ETA on the sea HUD, stock counts, amity numbers — so the same failure is
  waiting in every reader that takes its content from a whole-frame parse.
  - **Coarse is fine downscaled**: the family classifier (224x224), the minimap and
    shoreline tag readers, obstruction PRESENCE, chrome flags, "is a dialog up".
  - **Content is not**: any number or name a decision is made from.  Crop the region and
    read it at full resolution.
  - Measured on one Village Info trade list: the FULL 2400x1080 frame read `358, 102, 102`
    and the `44` on Matchlock Gun's thumbnail was NEVER PROPOSED AS TEXT — `parse_raw` at
    conf=0.01 shows only an `icon` box there, while Iron's `102` came back at conf 0.9999.
    The same detector on a 750x520 CROP read the `44` without trouble.  Not a threshold
    that ate it; it was never read.
  - **The two reads fail on DIFFERENT items**, so combine rather than choose: the crop got
    `44` right and misread Candle as `402`; the full frame got Candle right and lost the
    `44`.  Whole-frame authoritative where it has a value, region crop filling the gaps.
  - This is the missing half of *per-frame perception sharing* below, not an exception to
    it: share the whole-frame parse for cost and for coarse questions, and parse a REGION
    when its content decides something.
  - What it cost: the row parser drops a material with no number, so a missed two-digit box
    became a recipe with two of three materials — which then certified itself as complete
    (`_target_complete`) and was planned from for five runs.

- **Per-frame perception sharing** — capture the screenshot once per tick,
  pass the same `Image` and reuse `parse_fast_cached(img)` /
  `_ocr_frame(img)` results across detectors.  Don't re-shoot or re-parse
  inside helpers — each capture+OmniParser pair costs ~2-3s.  When a
  function needs vision, accept `frame` as an argument; only call
  `capture_screen()` at the top of the loop.
- **Transactional primitives gate on location** — any function that taps
  at a fixed sea / port HUD coordinate (rudder, L/R arrows, harbor
  depart, market buttons) MUST call `_assert_on_sea` / `_assert_at_port`
  before tapping.  A rudder tap fired on the wrong screen (post-arrival
  port_overworld) opened an unrelated quest dialog once — gating is the
  fix.  See `actions/sea_actions.py::_assert_on_sea`.
- **All screen input goes through `actions/ui.py`** — never hand-roll taps, swipes,
  key presses or waits in new code.  `ui.tap_element` / `ui.tap_text` take a DETECTED
  element or a label, `ui.scroll` / `ui.back` / `ui.settle(kind)` carry the jitter, and
  `ui.tap_at(x, y, why=…)` is the loud, logged exception for genuinely calibrated HUD
  controls.  This exists because the two habits it replaces each caused a live failure:
  a `time.sleep(1.0)` scroll loop tripped the ANTI-CHEAT and terminated the game
  (2026-08-21), and hardcoded coordinates silently mis-tap whenever the game re-bakes
  its camera-cutout offset — the same Village Info panel rendered ~110px apart between
  two sessions, so every absolute band in the trade-list parser rejected every row.
  Rule of thumb: **if you are about to write a number that means "where on the screen",
  find the element instead.**  Layout is stable; position is not.
- **Unit tests never touch the phone** — `tests/conftest.py` blocks any `adb`
  subprocess and hands tests offline stand-ins (a blank 2400x1080 frame, a canned
  `dumpsys` rotation, recording stubs for `adb_actions` input and the sea-steering
  press-and-holds).  A test that genuinely needs hardware must be marked
  `@pytest.mark.device` and is skipped unless you pass `--device`.
  This is not hygiene for its own sake: before the guard existed the suite fired 6
  `input swipe`s and 2 `keyevent 4`s at the live game — `test_hug_shore_bug2_wall_ahead`
  drove `HugShoreGoal.tick()` into the real steering primitives, and
  `test_village_recognition` drove `SailToGoal` into a real `press_back()`, which on the
  port overworld raises "Exit Game?".  Running the unit suite steered the ship and
  panned the map.
  Two corollaries when writing tests:
  - **Reset session state you depend on.**  Production caches are keyed by frame bytes,
    and every test builds the same blank frame, so they collide.  `_OBSTRUCTION_NONE_CACHE`
    (5s TTL) made four interruptor tests order-dependent — each passed alone and failed in
    the file.  conftest clears the known frame-keyed caches, the daily-news suppression
    global, and the Moondream family cache; add new ones there as they appear.
  - **State every attribute a `MagicMock` stands in for.**  MagicMock auto-vivifies
    unknown attributes as TRUTHY mocks, so when `has_hamburger` was added to the chrome
    detector every existing chrome mock silently began reporting "this is an overworld"
    and the Home-button branch became unreachable in tests — in two separate files.

- **Expected screens are multi-anchored; positive-button search is for the UNEXPECTED** —
  on a screen the bot MEANT to be on, an action must be anchored by several things at once:
  the screen's identity, the specific control (detected, not remembered), and the expected
  post-condition.  Searching for "the positive button" there is a guess dressed as a plan.
  `find_positive_button_for_context` is for the other case only: something unexpected
  interrupted a goal the bot was PURSUING and had already COMMITTED an action toward, so a
  transaction is genuinely half-finished and the goal says which button closes it.  With no
  goal there is nothing to finish and the search degrades to "tap whatever looks positive" —
  live 2026-08-22 that tapped `Trade Info` and a panel title on the Market landing page,
  because POSITIVE_LABELS contains the word "trade" and matching is by substring.
  In this game a POSITIVE button is identified by its **yellow/gold background**, not its
  wording (measured: the dialog OK is 64-91% gold pixels; Trade Info, Trade Points and
  Requested Trade Goods are 0%).  `brain.commit_actions.has_positive_background` enforces
  that; the word lists are a weak secondary signal and should eventually be LEARNED from
  observed real commit buttons rather than hand-listed.
  Today FOUR different notions of "positive" coexist — `POSITIVE_LABELS` (substring),
  goal keywords + `UNIVERSAL_COMMIT_LABELS`, `detect_commit_buttons` (colour+layout),
  `_DIALOG_OK_WORDS`, plus `_tap_depart_button`'s own "depart"/"supply" scan.  That is the
  "one canonical implementation per concern" rule being violated, and it is why fixing the
  trade false positive in one place left the escalation path exposed.

- **ONE sell flow, several goals** — selling at a market is a single flow: switch to the
  Sell tab → stage goods → tap Sell → handle Confirm/Result/Negotiation.  What differs
  between callers is only WHICH goods and HOW MANY of each:
    - `goal="profit"`  — every profitable good            [trade runs]
    - `goal="clear"`   — every non-kept good              [free cargo for a barter]
    - `goal="down_to"` — trim each good to a target qty   [surplus of a MATERIAL]
  Today `sell_goods` and `sell_down_to` are two implementations of that same flow, and the
  cost is concrete: the "N / M" Cargo-bar confusion had to be found and fixed TWICE (once
  when opening the quantity dialog, once when verifying it closed), and only one of the two
  paths backs out of the market on abort — so a false abort there DISCARDS a basket that
  was correctly staged (live 2026-08-22: 981 Ebony loaded, Sell one tap away, thrown away).
  Selection is the part that should vary; staging, committing and the dialog chain are the
  part that must not.  Same rule as "one canonical implementation per concern" — when a new
  selling need appears, add a GOAL, do not fork the flow.

- **ONE loop — the task drives the state machine** → `docs/one_loop_task_drives_state.md`.
  Two things are going on when the bot acts: a TASK (which owns the *sequence*: plan →
  gather → barter → route+sell, tracked in `brain/mission_progress.py`) and a UI STATE
  MACHINE (port_overworld / harbor / market / sea / village — which owns *no* sequence,
  only "where am I and what can I do here").  Today the task is chopped into sub-loops
  living INSIDE actions, each with its own private recovery policy — that inverts the
  hierarchy, and a low-level action ends up deciding the fleet's position in the world
  without knowing what the bot is trying to achieve.
  Measured cost in one day (2026-08-22): `open_world_map`'s loop judged `village` "not
  port/sea" and pressed Back until the fleet was AT SEA — leaving the village the mission
  had just sailed to; `navigate_to_building("harbor")` saw "sailing to Melanesian Village",
  judged it "not the harbour", and pressed Back, cancelling a departure that had SUCCEEDED
  — four times, 18.5 minutes; `_read_owned_via_sell` tapped a label matching the page title
  and left the market it was standing in.  Each was locally reasonable and globally wrong,
  because locally was the only scope it had.
  Rules: a primitive **never** presses Back to make the screen match its expectation (the
  screen is ground truth; the stale expectation is what updates) and **never** walks the
  fleet out of a settlement.  A loop inside a primitive may only wait for that primitive's
  OWN effect, and **THE BOUNDARY IS THE SCREEN** (user, 2026-08-28):
    - ✓ "did the tab switch?" — Port to Explore, the SAME screen. The tap can be swallowed
      while the map is still settling, so repeating it at the same point is waiting for its
      own effect. `select_world_map_tab` does exactly this and is right to.
    - ✗ "did the map OPEN?" — a full-screen change is a TRANSITION, not an effect, and no
      primitive may wait on one. (This was the rule's own example until 2026-08-28, and it
      was backwards: `open_world_map` looped ten times waiting for a new screen, which is
      how a map-opener came to be holding an OS lock screen, a daily-news popup and an
      Investment Season banner it had no vocabulary for.)
    - ✗ "am I somewhere else now?" — the task runner's question, and always was.

  After ANY correction — state adopted, dialog cleared, popup dismissed — the loop
  **re-asks the task what to do next** rather than resuming a half-finished intention.
- **Can't see what you need? You are LOST — hand back, re-perceive, TRANSITION.**
  The screen is the truth; the bot's belief is what goes stale (user, 2026-08-24).  When a
  primitive cannot find the control it needs, the usual cause is not a bad read but standing
  in the wrong place.  `read_fleet_status` hunting the ☰ inside a MARKET is the shape of it:
  the ☰ exists only on the port overworld, so the fleet was never going to be readable there
  — and its answer, re-perceiving three times IN PLACE, only re-confirmed the same true
  screen.  **Re-perceiving tells you where you are; it cannot change it.**  What was missing
  was the move.
  So: the primitive REPORTS what it could not see (naming it precisely — `read_fleet_status`
  says "no ☰ there", which is what lets a caller tell a navigation problem from a reading
  one), the caller re-perceives with `where_am_i`, and then TRANSITIONS to the state the work
  requires via `brain.nav_step.reorient_to(state)` — a bounded loop over the single-move
  `step_toward`.  `ARRIVED` means the screen was re-read and agrees; anything else is a
  refusal to act on, never a reason to carry on as if the state had been reached.
  Measured cost of not doing this: FOUR consecutive live failures at one step (2026-08-24),
  each a genuinely different cause — an arrival gate hiding the ☰, the same gate clearing
  itself before the sweep looked, then a sub-menu with no ☰ at all.  Every layer rediscovered
  "something is in the way" separately because each owned its own private recovery policy.
  That is the argument for the one loop (below), not for a fifth patch.
- **Anti-cheat tap discipline** — the game fingerprints automated input:
  - **Never burst-tap** (≥ 3 taps in < 1 s).  Burst-tapping the L/R
    arrows caused the spiral that fired the anti-cheat popup.  Use
    `turn_left/turn_right(taps=N)`'s built-in inter-tap jitter; do not
    loop tap manually.
  - **Never run a fixed-cadence action loop** — anti-cheat detects
    regular intervals.  Always jitter sleeps (e.g.
    `random.uniform(low, high)`).
  - **Every ADB tap** already includes the 0.3-0.8s post-tap delay
    — keep that.
- **Decisions at the right level — escalate, don't absorb** — a primitive's
  job is to *do one thing* and *report what happened*, not to decide what
  to do next.  When a primitive can't complete (button missing, wrong
  state, unexpected popup), it must return a structured failure and let
  the caller — the FSM / planner / goal — decide whether to retry,
  fall back, escalate to Claude, or abort.  Symptoms of this rule being
  violated:
  - inner loop retries forever and the bot gets stuck;
  - "I'll dismiss any dialog I find" logic inside a market function
    (dialog handling belongs in `perceive` / interruptor layer, not
    inside `auto_buy`);
  - recovery path embedded in a building helper instead of in
    `brain/recovery.py`.

  Quick test: *"if this function fails, does anything above it learn
  about the failure?"*  If no, the level is wrong.
- **One canonical implementation per concern** — when you find yourself
  writing logic that looks like something else (dialog detection, state
  assertion, button-finding, back-to-overworld navigation), find the
  existing implementation and use or extend it.  If two have already
  diverged, figure out which is correct and consolidate.  Examples of
  shared concerns that have ONE home today:
  - dialog detection → `vision/region_detectors/DialogModel`
  - obstruction classification → `vision/obstruction_classifier`
  - state classification → `brain/perceive.classify_nav_state` +
    fingerprint registry
  - "am I at the right place to tap?" → `_assert_on_sea` /
    `_assert_at_port`
  - button finding → `actions/sail_actions._find_button` (OmniParser-backed)

  When the existing one doesn't fit your case, generalise it — don't
  fork a parallel version.
- **When refactoring existing offenders** — these rules are most useful
  when written, not when discovered after the fact.  If during a task
  I notice buried-high-level logic or duplicated concerns nearby:
  - **don't bundle the refactor with the current change** (surgical
    changes rule applies);
  - **flag it inline** — note the smell in the response and propose a
    follow-up cleanup commit;
  - **the user picks the priority** — sometimes the offender is fine
    to leave until it bites again.

## Architecture (skeleton)
```
Android Phone (game)
        │ ADB screencap
        ▼
   capture/          raw frame (2400×1080 PNG)
        │
        ▼
   vision/           L0–L4 perception pipeline → SceneInventory
        │            (family_classifier, chrome, OmniParser, OCR, KB cache, Claude Vision)
        ▼
   memory/knowledge/ port / building / market / scene records
        │
        ▼
   brain/            perceive → reason → act → learn
                     (FSM, planner, goal stack, plan completeness)
        │
        ▼
   actions/          tap / swipe / press_back, sail / market / world-map primitives
        │ ADB input
        ▼
Android Phone (game)
```

Detail:
- **★ NEXT-PHASE FOUNDATION (read first, 2026-08-09)** →
  `docs/next_phase_architecture_2026-08-09.md`.  Consolidated design for the next phase:
  the THREE knowledge layers (strategy → task → control), the self-correcting
  TASK EXECUTOR (done-conditions + attempt memory: remember what worked / didn't →
  no-progress→try-different→stuck→escalate), progress/change detection
  (`docs/state_change_detection.md` — structured, not pixel-diff), the tiered
  reasoning cascade (cache → Qwen → Claude, learn-once), the knowledge base
  (control/task/strategy KBs; play-learned + human text/URL), and the current-code
  gap map + build order (HUD readers → task executor → done-conditions → decision
  cache).  Supersedes/consolidates the earlier scattered design notes.
- **★ Perceive→Act→Verify act-layer substrate (PROPOSED, 2026-08-19)** →
  `docs/perceive_act_verify_substrate.md`.  The primitive-level realization of the
  self-correcting executor: every action declares an EXPECTED post-condition, then
  re-perceives and compares expected-vs-actual — no assumed-state blind actions.
  L0-L3 verification (frame_diff → verify.py → targeted → perceive), an outcome
  taxonomy (match / no-change / wrong-state / refused), and a 4-phase migration.
  Motivated by the blind-`press_back` "Exit Game?" loop; see
  `memory/feedback_perceive_act_verify_expected_vs_actual.md`.
- **★ Barter command flow — BUILT 2026-08-20, NOT YET RUN LIVE** →
  `docs/barter_command_flow.md` (full gap table + build order + what each piece does).
  One typed command drives the mission:
  `python run_barter.py "barter <good> at <village>[, then take the route <name> |
  and sail to <port>]"` (`--dry-run` plans without sailing).  Recipe = invariant
  materials + 6h-volatile quantities, so the plan CONSTRUCTS the live recipe
  REMOTELY from port before gathering — `actions/village_check.read_village_barter_remote`
  (Base + Barter tabs, scroll-accumulated, source pins learned, KB invariants written
  back).  The CHECK runs BEFORE the graph is built, because every node is
  parameterised by it.  Plan: `brain/barter_quantity.plan_barter_rounds` with
  `free_space = capacity − cargo − 7-day supply reserve`, bounded by the PEAK hold
  (`max(Σ needs, output)` per round — the gather must be carryable, not just the
  output).  Graph: gathers → **supply_verify** → village → barter → route/sail tail →
  sell (`brain/mission.MissionTail`).  First live mission completed 2026-08-20
  (+66.6M ducats); see `memory/project_village_find_worldmap_2026-08-20.md`.
  ⚠️ Unvalidated live surface: the Village Info panel navigation and the main-menu
  fleet read (`actions/fleet_status.py`) — supervise the first run.
- **★ Event selling — FIRST LIVE SALE 2026-08-24 (branch `even_selling`)** → flow and cues in
  `docs/trade_system.md` ("Selling into a Bazaar").  The fleet hubs at London, reads the
  **Trade Event Schedule** (`vision/trade_event_reader.py`, times are **Korean UTC+9**), and
  if a Bazaar for a category **in the hold** opens within 12h it sleeps, sails, and sells into
  the window (`brain/event_selling.py`, `actions/trade_events.py`, `run_event_sell.py`).
  Bremen: 1,644 Box of Nutmeg at 211% → **+595.5M ducats**.  Three rules earned live: the
  row's location pin only opens **Location Info** (its gold **Move** button sails); a 1-day
  ETA cannot be seen to FALL, so arrival confirms a short hop; and the sale is gated on the
  tile's **price index ≥150%**, never on the clock alone.  The event to sail for follows the
  CARGO — naming a category by hand sent the fleet after goods without checking the manifest.
- **★ Screen knowledge as a KB — panels, not loose elements (DESIGN, 2026-08-24)** →
  `docs/screen_knowledge_as_kb.md`.  On-screen information is organised into GROUPS tied to
  particular screens and regions, matched roughly by content — not scattered elements to be
  classified by size or by individual words.  Aim: a game KB an AI model infers against, so
  that wording/layout changes degrade a read instead of breaking it, since the FUNCTIONALITY
  is what stays constant.  Today's per-element word matching in the left-menu detector is a
  WORKAROUND at the wrong granularity (a reward panel is one group, not five rows).
- **★ Dialogs without sub-loops — the market as contexts (DRAFT, 2026-08-30)** →
  `docs/market_as_contexts.md`.  A UI framework's listener registry buys INVERSION OF CONTROL,
  not "no polling" — and we have no event source, so the substitute is the dispatcher's tick:
  register a handler against an OBSERVED CONTEXT and the waiting becomes central.  The pattern
  already exists twice (`world_map._HANDLERS`, `village._HANDLERS`); the MARKET is the only
  worked activity with **no context classifier**, which is exactly where the sub-loops still
  are (`sell_goods:205`, `buy_materials:116`, plus private dialog waiters).  Proposes the
  market's context vocabulary, and shows both of 2026-08-30's market defects becoming
  unreachable rather than guarded.  **Chosen as the case study** for whether the pattern holds;
  carries a 5-step migration starting with classify-only over recorded frames.

- **★ The plan is a checklist of observable conditions (DESIGN, 2026-09-01)** →
  `docs/the_plan_is_a_checklist.md`.  The decomposition is a FUNCTION of (business need, game
  knowledge, current state) evaluated on every consultation — not a graph computed once and
  walked — and an item is done when the WORLD says so, not when an action reported success.
  Both are Guiding Principle #2 applied to the plan itself.  Live 2026-09-01: `leg.done` was
  set from "the harbour confirmed the departure", so the mission believed the sail part of
  `gather:Barcelona` was behind it and offered BUY at sea, for twenty minutes.  *Departed* is
  not *arrived*.  The checklist STRUCTURE already exists (`SubTask.deps`; the gathers are
  unordered); what is missing is splitting a leg into its checkable parts — `at(Barcelona)`
  then `buy(Iron)` — and making `done` a predicate.  **BUILT**: each activity declares
  `CAN_START` and the dispatcher refuses work the world cannot start, asking the task once
  more and logging a defect if the second order is also unservable.  **OPEN**: the predicate
  half, and the intent graph it waits on.

- **★ Dialogs are windows, not screens — BUILT 2026-09-03** → `docs/dialogs_are_windows.md`.
  The game reproduces ANDROID'S dialog mechanics, closely enough that we reverse-engineered
  `FLAG_DIM_BEHIND` without knowing it: a dialog is a separate window over the activity's, it
  dims what it covers by a measured **x1.98** (`dimAmount ≈ 0.5`, and the scrims STACK), and
  the activity behind keeps its state while losing **window focus**.  That splits two
  questions the code had collapsed into one — `active_submenu()` still truthfully says
  *Barter* (a reading), and `chrome_is_dimmed()` says whether it can be acted on (focus).
  Collapsing them wedged the San Village mission: `_open_barter_panel` asked
  `on_submenu("barter")`, got a true answer to the wrong question, and reported "opened the
  barter panel" three times having tapped nothing.
  The dispatcher now OBSERVES the dialog once and hands it DOWN — `activity.on_dialog(dialog,
  goal)` — because only the activity knows what the buttons mean; unhandled falls back to
  `DialogModel.safe_exit()`, bounded, then `BLOCKED`.
  **The dispatcher ASKS, it does not decide**: the answer comes from
  `brain.game_rules.answer_dialog` — named rules, then the DEFAULT of the positive option
  (user, 2026-08-23: *"the default is OK unless it is spending red gem"*), then a refusal for
  red gems, which are real money.  Where the Android analogy STOPS: Back-fires-onCancel
  protects a human's intent from the framework, and here **the bot is the user** — cancelling
  is not neutral, it is refusing to play.  At San Village, Cancel returns to a dialog whose
  `Receive` cannot succeed with a full hold, so it LOOPS; the goods are discarded either way;
  and the run had already taken that trade three times.  Same rule as flow completeness above:
  *Back / Home = Cancel, not progress*.
  Supersedes dismissal-by-keyword for blocking dialogs, which failed three ways: it missed by
  vocabulary (`overflow_prompt` looks for "cargo is full"; the game said "Insufficient Empty
  Space"), its learned entries went stale the moment detection narrowed the OCR to the front
  card, and it could never cover a dialog nobody had met.

- **★ Capability registry — the table, FOR REVIEW (DRAFT, 2026-08-30)** →
  `docs/capability_registry_design.md`.  The concrete per-activity capability table behind the
  note below, plus the three-way split it rests on: WORLD (has the ☰, Back exits the game) /
  CHROMED SCREEN (the title bar is Back) / CONTEXT (drawn over something that stays visible).
  Chromed screens are easy — **their left menu IS the capability list** — so villages,
  markets and buildings are high-confidence rows.  The overworlds and the WORLD MAP are the
  hard ones and are marked for review: the port's building list varies BY PORT, and the map
  is neither world nor chromed screen, with almost all its capability living in its eight
  CONTEXTS rather than on the map itself.

- **★ What can be done HERE — activity capability registry (PROPOSED, 2026-08-30)** →
  `docs/activity_capability_registry.md`.  Activities are handed work they cannot do — check
  capacity or trim surplus at a VILLAGE — because an activity declares the screens it serves
  and a flat goal list, never the WORK it can perform.  The registry says what each place can
  do (village: barter/gift/recruit; market: buy/sell; the ☰ only on the overworlds), the
  dispatcher refuses to dispatch elsewhere, and **an undispatched work order is logged as a
  defect** rather than silently skipped.  Carries the overworld invariant — *overworld ⟺ has
  the ☰ ⟺ Back is unavailable*, the chromed title being the back button everywhere else —
  which already shows `sea` missing from `_NEVER_BACK_FROM`.  And the reframe that dissolves
  most of yesterday: **the task is gather, barter, sell; trim, supply and capacity are
  SUPPORT** — opportunistic when the place affords them, never mandatory legs.  Lives in the
  dispatcher layer, which is the only one holding both the activity and the work order.
  Supersedes the `SERVES_FOR` sketch in `docs/per_goal_serving.md`.  **Not decided.**

- **★ A graph for intents — navigation as a distinct role (OPEN DESIGN, 2026-08-30)** →
  `docs/intent_graph.md`.  The four intents are EDGES between screens, and an edge exists
  where the screen carries the control.  Setting a course happens in exactly one place, the
  world map — a market cannot open it and neither can a village — so "who may emit
  OPEN_WORLD_MAP" is the whole of navigation, not decoration.  Read as a graph, three live
  wedges in one day were one failure: no edge from here, and no search for a path.  Would
  subsume `_is_inside`, `_TAIL_CAN_SAIL_FROM`, `_NEVER_BACK_FROM` (a MISSING back edge, not a
  special case) and `tap_world_map_control`'s per-state branches.  **The graph gives the next
  intent, never a plan to execute** — otherwise it is the sub-loop this architecture exists to
  remove.  **Not decided.**

- **★ Where a goal can be done — per-goal serving (OPEN DESIGN, 2026-08-30)** →
  `docs/per_goal_serving.md`.  An activity declares ONE `SERVES` tuple for ALL its goals, and
  the goals do not have the same reach: `ArriveAshore` is served at a village, `ReadHold` is
  not (no ☰ there), and both are `AshoreActivity`.  Three live wedges in one day came from
  this — OPEN_WORLD_MAP from a sub-menu, OPEN_WORLD_MAP at a village, ReadHold at a village —
  each patched where it surfaced without writing down the fact underneath.  The note records
  the `SERVES_FOR` shape proposed in the moment AND why it may be the symptom: the fact is
  about CONTROLS (the ☰, the globe), not about goals, and it is already duplicated between
  `to_intent._is_inside` and `tap_world_map_control`'s per-state branches — which is exactly
  what let them disagree.  **Not decided**; the user asked for a deeper design first.

- Full vision pipeline → `docs/vision_pipeline.md`
- `where_am_i()` design + location vocabulary → `docs/where_am_i_design.md`
- Architecture overview (broader) → `docs/architecture_overview.md`
- **Layered bot architecture (task / reasoning / skills / perception) — TARGET
  design (2026-08-07)** → `docs/bot_architecture_layers.md`.  Task layer takes
  natural-language tasks (goals + schedule + concurrent monitors); reasoning/
  mission layer below owns "where am I + how to achieve the goal + recover";
  skills layer wraps the existing goals/actions; perception is the structured
  `PerceivedState` substrate.  The **reasoning fallback** (LLM over structured
  state + intent, whitelisted actions; fixes wrong-place + headless-escalation)
  → `docs/reasoning_fallback_layer_design.md`.
- **Perception consolidation (A0–A3, A2 structured PerceivedState) — IN PROGRESS
  (2026-08)** → `docs/perception_backlog.md` + `docs/a2_perceived_state_implementation_plan.md`.
  Family CNN owns coarse structure (chromed ≠ overworld); panel identity from the
  left-menu vocab; general `GridDetector` reads same-layout grids/lists; market
  goods reader is OmniParser-detected (no hardcoded grid).  Principle: **detect,
  don't hardcode; nav fast, buildings/port precise via OmniParser.**
- **Affordance learning & curiosity — grow the KB from play (design, 2026-08-07)**
  → `docs/affordance_learning_and_curiosity.md`.  OmniParser sees elements the KB
  doesn't (`Trade Points`); learn them by reasoning + **safe** experimentation
  (open info → Back), detect operation-completion signatures, record into an
  affordance KB.  **Safety-gated**: probe only reversible/info affordances; never
  experiment on spend/confirm/red-gem actions.  Extends the Data Flywheel +
  learn-once.
- KB record schemas → `docs/kb_record_schemas.md`
- Scene model substrate → `docs/scene_model_design.md`
- Dialog / event typed detectors → `docs/dialog_and_event_models.md`
- Sea capture loop + real-time plan → `docs/sea_capture_and_steering.md`
- Navigation models (mini-map + shoreline) — corpus, training, wire-in → `docs/navigation_models_status.md`
- **River/shore exploration — perception/mapping/planning split** → `docs/exploration_navigation_layers.md`
  (Read this before touching `JunctionDetector`, `JunctionGraph`,
  `TremauxPicker`, or `water_skeleton.py`.  Defines what each layer
  owns and what crosses between them.  The "perception is stateless,
  mapping owns memory, planning consumes both" principle is load-
  bearing for the replay-test framework.)
- **Tactical substrate — CURRENT (single hugging-side bankline, 2026-07-31)**
  → `brain/ai_nav/layers/loop_tactical.py` (`LoopTactical`, `--tactical
  loop`).  Design doc `docs/loop_navigation_design.md`; milestone
  `memory/project_loop_bankline_roundtrip_2026-07-31.md`.  The live
  substrate is ONE hugging-side bankline, not a whole loop:
  1. **LOOP = raw ship-water-CC contour** — full-coverage invariant (the
     water body the ship is on must be entirely in the loop; no erosion,
     which distorts/splits complex shapes like the strait).
  2. **Desired direction = ship HEADING** (perception, not a compass seed).
  3. **FOOTHOLD = forward-port arc scan** — sweep abeam→straight-ahead on
     the hug side, nearest LAND hit (reacts to land on the bow, not just
     abeam; excluding "behind" is load-bearing).
  4. **NAV LINE = bank forward-trace to the frame-edge exit / dead-end**,
     offset inward per-point by `min(hug≈14, DT)`, then pocket-cut + light
     DP.  Reflex 40px / tactical 80px along it; commit = ship→reflex.
  Dead-end/lake "falls out free" (the one bank curves around and back).
  **Validated end-to-end**: Cairo→lake→Cairo round trip, 734 ticks, 4
  flips, zero thrash — beats the prior best (861/13).  Steering: PID is now
  the live default (`--no-pid` for the sim).

  **[SUPERSEDED 2026-07-31] anchor substrate (bank tracer)** →
  `tools/bank_tracer.py` (`trace_bank_to_edge`) +
  `brain/ai_nav/layers/tactical.py` (`LookaheadTactical`, `--tactical
  lookahead`) — anchor policy (sticky / migrate / hold(pocket_ahead) /
  reach-gate) over a traced polyline ENDPOINT, which flipped at
  straits/islands/dead-ends.  Kept for the pytest suite + history; the
  loop substrate replaces it as the default going forward.  See
  `memory/project_reflex_wp_arclength_lookahead_2026-07-28.md`.
  Regression suite: `tests/tactical_scenarios/` — **perceive-from-frames
  replays** that drive the real `AiNavPipeline` over recorded frames via
  `FileVisionSource` (ensemble heading, dead-reckoned position), NOT
  injected trace values.
- **[OBSOLETE 2026-07-28] Edge-anchored centerline extraction** →
  `docs/edge_anchored_centerline_design.md` (was proposed to retire the
  skeleton substrate; itself superseded by the bank tracer above and
  never wired.  Prototype `tools/edge_centerline_prototype.py`.  Kept
  for history.)
- **[OBSOLETE substrate] LookaheadTactical SEARCH-mode tracker** →
  `docs/tactical_search_tracker.md` (frame-edge dest-continuity logic;
  its skeleton/centerline upstream is retired.  The "keep the dest if
  it's still on a water frame edge" idea survives in the bank tracer's
  frame-edge exits.)
- **[OBSOLETE substrate 2026-07-24] LookaheadTactical original walker** →
  `docs/lookahead_tactical_design.md` (skeleton-walk lookahead; kept for
  historical context).
- **Tactical LOCK → mission-layer refactor sketch** →
  `docs/tactical_lock_mission_refactor.md` (design draft, not yet
  implemented — LOCK decisions should live in mission not tactical).

## Coding Conventions
- Modules independently testable with mock ADB.
- Every ADB tap includes a small random delay
  (`time.sleep(random.uniform(0.3, 0.8))`).
- Screen templates live in `vision/assets/` as PNG.
- Game state is single source of truth — updated from vision output each tick.
- Log every action with timestamp, state before, action, outcome.
- Type hints throughout.
- FSM states in separate files under `brain/states/`.

## ADB Setup
```bash
adb devices
adb exec-out screencap -p > screen.png
adb shell input tap 540 960
adb shell input swipe 540 800 540 400 300
```
Phone resolution is fixed at **1080×2400 landscape**; templates break
if it changes.

## Key Design Decisions
- **scrcpy is for display only** — all input goes through ADB directly.
- **Port map over building list** — port map is clean and unambiguous.
- **Tiered vision, not raw OCR** — local cache → local models → Claude
  API, in that order.  See `docs/vision_pipeline.md`.
- **Learn once, reuse forever** — every Claude Vision call is cached;
  the bot gets cheaper to run over time.
- **Building types capture cross-port knowledge** — a Cathedral works
  the same in every city.
- **Markets are always port-specific** — prices fluctuate; always
  re-read, always append to history.
- **Phone runs the game, Mac runs the bot.**
- **OCR engine choice** — EasyOCR for the user-language UI; PaddleOCR
  with multilingual model for player-generated text (CJK / Cyrillic /
  Arabic) — see `docs/ui_anatomy.md`.

## Game Knowledge (load on demand)
Per-system detail kept out of this file to keep context light:
- Game basics, nations, growth tracks → `docs/game_mechanics.md`
- Trade system (price signals, trends, mate abilities, market events) → `docs/trade_system.md`
- Combat (modes, Repel Support, ceasefire) → `docs/combat_system.md`
- Exploration, fishing, port investment → `docs/exploration_and_investment.md`
- UI anatomy (port screen, sea HUD, overlay vs modal) → `docs/ui_anatomy.md`
- Tests come in two kinds: `tests/` is UNIT (fast, no pixels); `tests/functional/` runs
  against REAL CAPTURED FRAMES and skips when they are absent — the frames live in
  `tests/stage_suite/frames/`, which is NOT in the repo.
- Perception as DATA, not flow: one owned observation, generation-based staleness,
  invalidation from the action layer → `docs/perceive_repository.md` (DRAFT, not decided)

## Milestone Status
One-line snapshot.  Full detail in `docs/milestones_status.md`.

| # | Milestone | Status |
|---|---|---|
| 1 | Core Infrastructure | ✅ DONE |
| 2 | Sea Navigation & Basic Trading | ✅ DONE |
| 2.5 | Data Flywheel | ✅ DONE |
| 3 | FSM & Robust Recovery | ✅ MOSTLY DONE — Planner / Scene Model / typed detectors / flow + plan completeness shipped.  `recovery.py` cleanup (task #25) still open. |
| 4 | World Map Intelligence | ✅ PARTIALLY DONE — pan / zoom + village support + lat-lon localisation shipped.  City-info reading + multi-hop routing open. |
| 5 | Route Optimisation | Not started |
| 6 | Port Knowledge Base & Discovery | ✅ PARTIALLY DONE — `explore_port`, KB expansion via Claude consult, first-visit overlay handling shipped. |
| 7 | Combat & Sea Exploration | ✅ PARTIALLY DONE — mini-map detector (F1 0.74) + shoreline classifier (F1 0.65) trained and wired into `BotObservation.minimap` / `.shoreline` on sea ticks.  `MinimapNavigationView` (classical image-processing implementation of the NavigationView Protocol) shipped: 8-sector land readings + ship heading + bow-clearance.  `HugShoreGoal` is the first sea-control consumer — calibrated press-and-hold steering (~120°/sec), closed-loop mini-map polling at sub-1s cadence, bow `land_fraction` channel direction.  **MILESTONE 2026-06-02 — first end-to-end autonomous destination reach**: Cairo (30.2, 30.4) → (5.71, 32.12) in 506 ticks, phase=COMPLETE.  Bug2-style commitment via §13.21 destination-anchored tangent + §13.25 clearance term + forward-arc-only avoider (§13.26 removed) + §13.27 stuck-detector/UTurnRecovery as safety net (didn't fire).  See `memory/project_first_autonomous_destination_reached.md`.  **Earlier milestone 2026-05-28: autonomously hugged into Nile river origin** (`memory/project_nile_river_reached.md`).  See `docs/navigation_models_status.md`, `brain/goals/hug_shore.py`, `brain/goals/uturn_recovery.py`.  Outstanding: combat-mode steering, voyage smoothness tuning. |
| 8 | Autonomous Company Growth | Not started |

**Currently in flight:** **MILESTONE 2026-09-04 — the barter mission runs END TO END on
the dispatcher path, first time with no relaunch.**  `hutu_groundnut` from London: plan →
`gather:Faro` → `gather:Madeira` → supply → `sail_to_village` → 3 barter rounds at Hutu
(amity to 100,000, 3×1,036 units) → **route home** → sold at London at 40,600 profit/unit,
~126M ducats.  59 steps, 82 minutes, `every leg is done`.

Two fixes that had only ever been checked against recorded frames ran live and worked:
  * **the search box is not the port** (`17216da`) at FARO — the case that wedged the
    morning's mission, where the typed prefix IS the whole name, so the box scored 1.00
    against the row's 0.62.  `Skipping 'faro' @ (303,144) … Matched 'Faro' @ (209,200)`.
  * **a saved route commits with a bare `Move`** (`21e88f8`) — `sail_route done` appears
    for the FIRST time in 184 session logs.  Every route that ever sailed before went via
    the deprecated `actions/route_execution.py`, which is what hid the gap.

STILL OPEN, and none of it was needed for this run: `EXIT_BUILDING` is not retried at a
village (`823547b`, deliberate — a stray second Back loses the village, but on a sail leg
leaving IS the goal); the sea wake-timer sets its next sleep from a frame captured BEFORE
that sleep; `_answer_it_anyway` taps a computed point rather than the button it located;
and `run_barter.py` / `run_task.py` index different task sets, the latter still on the
deprecated recovery path.

**Previously in flight:** **MILESTONE 2026-07-31 — single hugging-side
bankline tactical, validated end-to-end.**  Rethought the whole-loop
substrate into ONE hugging-side bankline (`LoopTactical`, `--tactical loop`
— see the "Tactical substrate — CURRENT" pointer above): raw ship-water-CC
loop (full-coverage invariant) + forward-port arc-scan foothold (nearest
land abeam→ahead) + DT-offset nav line to the frame-edge exit/dead-end.
Direction from the ship heading; dead-end/lake "falls out free".
**Live voyage (session `loop_consolidated_2026-07-31`):** Cairo (29.95) →
equator → **lake terminal (-1.27)** → clean U-turn → back to Cairo → on N
into the Med.  Round trip **~734 ticks, 4 commit-flips, ZERO thrash** —
beats the prior best round trip (861 ticks, 13 bounces, WITH PID; this run
had no PID).  The Y-tip/lake terminal, where every earlier tracker thrashed,
was clean.  **Steering: PID is now the live default** (linear RATE_DPS=120
under-turns; `--no-pid` for the sim) — see
`memory/project_steering_pid_calibration_2026-07-28.md`.
**LESSON: validate DIRECTION, not just flips** — an interim version scored 0
flips while going ~180° backward in channels.
See `memory/project_loop_bankline_roundtrip_2026-07-31.md`.
**Tests:** `tests/tactical_scenarios/` are live-run-style perception
replays (real `AiNavPipeline` + `FileVisionSource`, ensemble heading,
dead-reckoned position) — still wired to `LookaheadTactical`; the loop
substrate is validated via `tools/viz_fullcov.py` + `data/loop_scanfoot_report.html`.

Prior milestone (v0.8.0-cnn-heading, 2026-06-30): heading CNN replaced
template-match + self-healing stack (ship-only training, 220K-param
convnet).  See `memory/project_cnn_heading_milestone_2026-06-30.md` and
the `v0.8.0-cnn-heading` git tag.

Next push candidates: Y-tip strategic memory (Trémaux) for the residual
strategic-layer thrash; a resupplied voyage to confirm the full
in-and-out dead-end traverse live; combat-mode steering on the same
substrate.
