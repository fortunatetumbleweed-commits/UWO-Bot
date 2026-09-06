# Dialogs without sub-loops — the market as contexts

**Status: DRAFT 2026-08-30; four live failure cases recorded 2026-09-05.** The market is the
chosen case study: it is the last activity still driving whole flows internally, so converting
it is how we find out whether the pattern holds. It is no longer speculative — an evening of
runs produced four distinct failures that the pattern would have made unreachable, and they
are recorded below as verification anchors. Branch: `market_refactor`. Companion to `docs/capability_registry_design.md` and `docs/intent_graph.md`.

> *"in Android or any UI framework, you register handlers for UI controls, so a click on a
> button does not need to go through the intent dispatcher, but it is a listener registry that
> can call back when the action is performed. So you do not need a loop to wait for that. I am
> wondering how that paradigm can be used for subloops still in market."* — user, 2026-08-30

## The architecture: no sub-loops at all

**The cycle, and there is only one** (user, 2026-09-05):

```
dispatcher   perceive  ->  dispatch
activity     handle the perceived request  ->  modify context  ->  take action  ->  hand back
```

*"Thus it never swallows anything."* That sentence is the whole justification, and it is a
property of the SHAPE, not of any handler's care. Every screen the game puts up passes
through `perceive` before anything acts on it, because there is nowhere else for control to
be. A dialog cannot be tapped through unseen; a dropped tap cannot be mistaken for a state
change; an unexpected screen cannot be reasoned about by code that never asked what it was.

**This is stricter than "a loop may wait for its own effect".** CLAUDE.md's Principle #5
permits that narrower form, and it is what the remaining loops appeal to. But a loop waiting
for its own effect still holds control while the screen changes under it, and holding control
is exactly what swallows. FC-3 is the proof: `commit_via_positive_taps` was waiting for its
own effect — the commit cycle closing — and in the meantime tapped `Receive` and then `OK` on
a discard notice, so the dispatcher never got a tick and the overflow handler was never a
possibility. Nothing in that loop was careless; the loop itself was the defect.

So the target is not "fewer sub-loops" or "sub-loops that behave". It is **none**, and the
test is mechanical: *can control be inside this function while the screen is showing something
it did not expect?* If yes, it is a sub-loop whatever it is called.

### What this costs, and why it is affordable

One tick per action instead of one tick per flow. The Performance section below bounds it: a
tick is **one observation → one decision → the actions that decision entails**, so taps
justified by the SAME observation still go together, and the cost is perceives, not taps.

### It applies to the village's retry too

`actions/barter_panel._try` is quoted below as the reference for handling an unresponsive
tap, and its DOCTRINE is right — read back, compare a signature, and answer `None` for "the
tap never landed" rather than `False`. Its LOOP is not:

```python
for attempt in (1, 2):          # <- a sub-loop, by the rule above
    ui.tap_at(...)
    reading = read_barter_panel(screen().get(...).frame)
```

Control sits in that function across a capture, so a dialog arriving between the two attempts
is tapped over rather than seen. Under this architecture the retry is not a loop at all: the
handler taps, returns, and the NEXT tick either sees a changed screen (done) or the same one
(tap again) — with the bound living on the activity, keyed to the goal, as `_selects` and
`_committed` already do. Same doctrine, no loop, and the dialog case comes free.

The village is the model for HOW to judge a tap. It is not yet the model for WHERE the
waiting happens.

## What a listener actually buys

Not "no polling" — **inversion of control**. The code stops asking *has it happened yet?* and
declares *when it happens, do this*. Waiting becomes the framework's problem, because the
framework OWNS the event: it delivered the click, so it knows.

We have no such source. Nothing calls us back; we tap through ADB and read pixels
(Guiding Principle #0). So `setOnClickListener` cannot be lifted directly — there is no event
to register for.

**The substitute is the dispatcher's tick.** It perceives every cycle anyway. Registering a
handler against an OBSERVED CONTEXT gives the same inversion of control, with the loop owned
centrally instead of privately. That makes our version closer to a **reconciliation loop**
than to a callback — the handler fires on state, not on delivery — which is the honest shape
for a system that can only look.

## The pattern already exists here, twice

```python
brain/activities/world_map.py:745   WorldMapActivity._HANDLERS = {context: (handler, goals)}
brain/activities/village.py:196     handler = self._HANDLERS.get(local)
```

Classify the context → look up the handler → do ONE thing → return. The dispatcher perceives
again and the next context resolves. No private loop, and an unrecognised context is handed
back rather than acted on.

The world map and the village were converted. **The market never was**, and that is exactly
where the remaining sub-loops live:

```
actions/sell_goods.py:205      for r in range(max_rounds):     stage / commit rounds
actions/sell_goods.py:231        for g in sellable:            tap each tile
actions/buy_materials.py:116   for material in targets:        "round 1/47"
```

## What the market looks like today

`MarketActivity.work` branches on the GOAL and then calls a whole flow:

```python
if isinstance(goal, Hold):      return self._buy_toward(goal, port)   # → buy_to_goal(...)
if isinstance(goal, FreeHold):  return self._sell_off(...)            # → sell_goods(...)
if isinstance(goal, TrimHold):  return self._trim_to(goal, port)      # → sell_down_to(...)
```

There is **no `market_context.py`** — the market is the only worked activity with no context
classifier. Its dialogs are handled deep in the action layer instead:

```
actions/buy_materials.py:174   _react_after_purchase()        waits for confirm → result
actions/sell_goods.py:544      _dismiss_dialog()
brain/commit_actions.py:360    commit_via_positive_taps()     taps until the cycle closes
```

Each of those is a private loop waiting on a screen change — the thing Principle #5 forbids,
surviving because the market was never converted.

## The proposed vocabulary

A first cut, to be corrected against frames:

| context | what it is | one action |
|---|---|---|
| `market_landing` | `building:market`, Purchase / Sell in the left menu | open the tab the goal needs |
| `purchase_page` | the buy grid | stage the next material, or commit |
| `sell_page` | the sell grid | stage the next good, or commit |
| `quantity_dialog` | the amount prompt | type the amount, confirm |
| `confirm_dialog` | "are you sure" | OK |
| `result_dialog` | **the proof** — Total Amount, Profit, Balance | READ it, record, OK |
| `negotiation` | the haggle prompt | accept / decline per policy |
| `restock_prompt` | the gem refresh | blue gem yes, **red gem never** |
| `MISS` | none of the above | hand back — the dispatcher owns it |

**CORRECTED AGAINST FRAMES, 2026-09-05.** Three contexts were missing and one rule was
implicit:

| context | what the game actually draws |
|---|---|
| `overflow_prompt` | title **"Insufficient Empty Space"**, a `Received Trade Goods` strip, a `Cargo` strip, a `Receive` button |
| `discard_notice` | **"Complete the trade? N X has not been claimed yet. Unclaimed trade goods will be discarded."** Cancel / OK |
| `cargo_full_notice` | **"The Cargo Hold's Trade Goods slot will be exceeded by N slots"** |

**CLASSIFY BY STRUCTURE AND POSITION, NEVER BY WORDING.** FC-3 is the whole argument: the
village's `OVERFLOW_PROMPT` keys on `("overflow", "exceeds", "cargo is full")` and the game
says *"Insufficient Empty Space"*, so the handler has never once been reached. The words were
guessed; the layout was not. Each row above is identifiable by what it CONTAINS — a titled
card with a `Received Trade Goods` strip over a `Cargo` strip is that dialog whatever it is
called — and `DialogModel` already segments the card from the brown title bar. Wording may
rank candidates; it may never be the test.

Goals stay as they are (`Hold`, `FreeHold`, `TrimHold`, `SellHold`) and become the second key,
exactly as `(handler, goals)` already works on the world map: a context that cannot serve the
current goal hands back instead of acting.

## What this dissolves, rather than guards

Two defects from 2026-08-30 stop being reachable. The four cases recorded below are
the current evidence, and each carries its own "what the refactor must show":

- **`Im glad` staged as a good.** The reader parsed a goods grid while a Result dialog was up
  — the dialog draws inside the goods zone, so a dialog row became a tile. As contexts,
  `result_dialog` classifies FIRST and "read the goods page" is not reachable from it. The
  price guard added on 2026-08-30 stops the tap; the context model stops the read.
- **`sold` reporting what was STAGED.** The list is built when tiles are tapped and never
  revisited. `result_dialog` is the natural and only place to record what actually sold —
  from `Total Amount`, which CLAUDE.md already names as the proof a transaction happened.

## Failure cases the refactor must dissolve

A growing list, kept because each one is a VERIFICATION ANCHOR: the migration is not done
until the case is unreachable, and each is concrete enough to replay from its recorded
session. They are evidence, not a backlog — a fix that guards one of these without
dissolving it has not done the work.

### FC-1 — a dropped OK left a dialog nobody could see (live 2026-09-05, Madeira)

Session `data/sessions/trace_barter_cmd_2026-09-05T19-58-09`, log `/tmp/san_run1.log`,
20:43:04 onward. Buying Raisin, 14 refresh cycles in.

```
20:43:04  [refresh] tap Replenish-Stock OK @ (1309, 827)
20:43:12  [refresh] verify: accepted=True timer 00.28.50→?? → refreshed=False
20:43:12  no refresh for 'Raisin' — refresh NOT confirmed; the shelf stays empty
20:44:10  gather:Madeira done
20:44:10  sell_surplus happens inside a market and there is none on 'unknown'
          FAILED at step mission
```

**The tap was correct.** Measured on the screen afterwards, the dialog's OK button is at
(1307, 826); the bot tapped (1309, 827). The game simply did not register it — the ~1-in-20
dropped tap this codebase already documents. The Replenish Stock dialog was still open when
the process exited, 90 minutes later.

**Four things then went wrong, and every one is a consequence of the sub-loop:**

1. **The verify was blind BY CONSTRUCTION.** It confirms a refresh by reading the good's tile
   or the restock timer — both of which the dialog covers. A stuck dialog and a failed
   refresh produce identical readings, so the sub-loop cannot tell them apart even in
   principle. As contexts, the dialog classifies first and the question never arises.
2. **Nothing dismissed the dialog.** `refresh_market` owns a private dialog waiter; when its
   expectation is not met it returns False and the loop breaks, leaving the dialog on screen.
   A dispatcher tick would have perceived it and routed it to `on_dialog` — the machinery in
   `docs/dialogs_are_windows.md` that every other activity already gets for free.
3. **No retry of a tap that plainly did not land.** One re-tap on a re-read position is the
   documented remedy for a dropped tap, and there is nowhere in a sub-loop that owns it.
4. **The failure surfaced three steps away from its cause.** The leftover dialog made the
   next leg's location read `'unknown'`, `port_is_underfoot` returned False, and the mission
   died with a message about markets and locations. Nothing in that message points at a
   dropped tap in the previous leg.

**What the refactor must show:** with the market as contexts, a `replenish_dialog` context
classifies before any goods read, its handler answers or re-taps, and a dropped OK costs one
tick instead of a mission. The anchor is FC-1 replayed from its frames: the run reaches the
village.

**Cost of this instance:** the mission failed one leg from San Village with 1,828 Pig and
1,540 Raisin aboard — everything needed for all six planned rounds.

### FC-2 — a purchase that never happened, recorded as if it had (live 2026-09-05, Madeira)

Session `data/sessions/trace_barter_cmd_2026-09-05T21-02-24`, log `/tmp/san_run2.log`,
frames 68 and 75. Found by the user watching the screen: *"it is not hitting Purchase button,
but hitting the Purchase submenu, and also did a refresh when the card has raisin to be
bought."*

```
21:11:20  tap (1009, 554)        load Raisin — the goods tile        (frame 66)
21:11:29  tap (1959, 997)        "tap Purchase" cost=63360           (frame 68)
21:11:57  [ledger] bought Raisin, amount unreadable — pending
21:12:04  tap (1399, 160)        the refresh icon                    (frame 75)
21:12:11  tap (1309, 827)        Replenish-Stock OK
21:12:20  no refresh for 'Raisin' — refresh NOT confirmed
```

**Frame 75 proves the purchase never happened.** Taken immediately before the refresh tap, it
still shows **Raisin 110 staged in the cart** with its remove-X, the purchase bar still
carrying a price (58,410), and cargo Raisin unchanged at **1,540**. Nothing had left the cart.

**The purchase bar is TWO controls and the tap lands on the seam.** Its left half is the price
field (`63,360`), its right half the `Purchase` label; (1959,997) falls just inside the PRICE
half. The same coordinate has committed purchases on earlier cycles, so this is a FRAGILE aim
rather than a wrong one — the same shape as the route row landing on a divider in
`memory/the-rail-belongs-to-the-lit-tab`. Aim at the label's own element, not the bar's middle.

**Then three things compounded, and each is the sub-loop's doing:**

1. **A purchase was RECORDED without proof.** CLAUDE.md's rule is that the RESULT DIALOG is
   what says a transaction happened. No dialog appeared — nothing had been bought — and the
   ledger recorded `bought Raisin, amount unreadable` anyway. The bot then believed it held
   an unknown extra quantity it did not have.
2. **It refreshed the market with the cart still staged.** The cart is PANEL data and dies
   when the market restocks, so a refresh here discards the staged goods. Nothing asked "is
   there anything staged?" before spending a gem.
3. **No re-tap of a commit that plainly did not land** — the cart still holding the goods IS
   the observation that says so, and it is on the same frame the loop had in hand.

**What the refactor must show:** as contexts, a staged cart is a state — `goods_staged` — and
committing is the only thing reachable from it. A commit that leaves the cart staged is not a
completed purchase, and the ledger records only what a `result_dialog` context confirms. The
anchor is FC-2 replayed from its frames: the cart empties, or the run does not claim it did.

**Cost of this instance:** a wasted refresh gem, a ledger that over-counted Raisin, and a leg
that ended believing it had bought something.

### FC-3 — the overflow handler is unreachable, twice over (live 2026-09-05, San Village)

Session `data/sessions/trace_barter_cmd_2026-09-05T21-38-09`, frame 214. Six barter rounds,
hold at 4,952/4,952:

```
Insufficient Empty Space — "Cannot receive item due to insufficient space."
Received Trade Goods: 360        Cargo: 241 water · 248 food · Pig 1 · Raisin 1 · Groundnut 4,461
```

Overflow 360 against materials of 1+1, so `overflow_dialog`'s first last-round condition —
*the overflow exceeds every material aboard* — was met outright, and both should have been
dumped. Nothing was dumped. The run discarded 360 units here and 400 earlier, silently.

**TWO independent barriers stood between the dialog and the handler, and fixing either alone
leaves the other:**

1. **The commit sub-loop never handed back.** `commit_via_positive_taps` (`max_taps=6`,
   *"re-captures and re-parses at the top of every iteration"*) was mid-iteration tapping
   positive buttons — `Receive`, then `OK` on *"Unclaimed trade goods will be discarded"*. The
   dispatcher got no tick while the dialog was up, so **nothing classified the screen at all**.
2. **The keywords do not match.** Classifying frame 214 offline returns `village_top_menu`,
   because `_OVERFLOW_WORDS = ("overflow", "exceeds", "cargo is full")` and the game says
   *"Insufficient Empty Space" / "insufficient space" / "organize your Cargo Hold"*. Not one
   word overlaps.

Barrier 2 is the SAME miss `docs/dialogs_are_windows.md` records as the motivation for the
window model — *"The dialog says 'Insufficient Empty Space'. Same meaning, no match."* It was
written down and the keyword list was never changed. That is the argument against classifying
any dialog by wording, in the market's new table as much as the village's old one.

**FIXED 2026-09-06, AND NOT WHERE THIS DOC PUT IT.** FC-3 was filed under the market. It is
not a market failure at all — the market is never involved. `VillageActivity._on_confirm` and
`._on_result` called `commit_via_positive_taps`, the LOOPING form, from inside handlers that
are otherwise perfectly context-driven:

```
[commit] iter 1: tap 'Ok'
[commit] iter 2: tap 'Receive'     <- the overflow card, which had just appeared
         ...     tap 'Ok'          <- "Unclaimed trade goods will be discarded"
```

Three screens, one handler call, no dispatcher tick — so `_on_overflow`, which owns that card
and holds the dumping policy, was never reachable. The sub-loop was hiding INSIDE the
activity this doc holds up as the reference implementation.

The fix is `commit_actions.tap_one_positive` — press the positive control once and hand back
— used by both handlers. The looping form survives for the callers that genuinely want it
(`verified_recruit`, `human_escalation`, `plan_actions`, `barter_executor`), where an
escalation path owns the screen until it settles.

**Two corrections to this doc follow:**

* **Step 4's gate was wrong.** It read "FC-1 and FC-3 replay to a different outcome". FC-3
  cannot gate a market step. FC-1 still can.
* **Step 7 — "delete the private waiters" — is not available for
  `commit_via_positive_taps`.** It has eight-plus call sites across the codebase and is
  general infrastructure. Either callers opt into a one-shot mode, as the village now does,
  or each converts on its own schedule; the loop itself stays.

**What the market refactor must still show for the overflow:** the dialog is classified
before any positive button is pressed, and the handler that owns it decides. That remains
true — it is simply the VILLAGE that proves it first.

### FC-4 — a sell tap that did not register, and a basket nobody checked (live 2026-09-05, London)

Same session, frame 255. The mission's payoff: 4,461 Bambara Groundnut at 39,642 profit/unit,
about 177M ducats.

```
22:31:40  tap (1450,314)   "load-to-sell Bambara Groundnut"
22:31:49  sold at London: nothing
          blocked: 'no Sell button after loading basket'
          FAILED at step mission
```

**The aim was right.** `(1451,316)` — one pixel away, same tile slot, same screen — staged
and sold Argan Oil at London earlier the same evening, and `(1010,314)` sold Almond. The tile
BODY is the control on the sell grid. The tap simply did not register.

**And the proof was on the frame in hand:** the right-hand panel read *"Select the goods
you'd like to sell."* Nothing staged. The loop did not look; it went hunting for a Sell button
that only exists once something IS staged, then reported THAT as the failure — a message
about a missing button, three steps from a tap that was swallowed.

**What the refactor must show:** staging is verified against the basket, a tap that changed
nothing is re-tapped, and `Load All` (present on this screen) is available as the batch move.

## The common cause: a dropped tap treated as a state change

All four cases are the same failure wearing different clothes, and **none of them is an
aiming bug**:

| | tap | evidence it was correct |
|---|---|---|
| FC-1 | Replenish `OK` (1309,827) | measured afterwards: the button is at (1307,826) |
| FC-2 | `Purchase` (1959,997) | the same point committed purchases in earlier cycles |
| FC-4 | sell tile (1450,314) | (1451,316) sold Argan Oil an hour earlier |

The game drops roughly **one tap in twenty**, which this codebase has documented since
2026-08-26. That rate is not a defect; it is the environment. The defect is that each
sub-loop treated the tap as having happened and moved on to the NEXT control — and in every
case the disproof was already on the frame it held:

```
FC-1   the Replenish dialog still open
FC-2   the goods still staged in the cart, the price still showing
FC-4   "Select the goods you'd like to sell."
```

### The village already answers this, and the market must inherit it

`actions/barter_panel._try` is the reference for the JUDGEMENT — not for the loop it is
written in, which the architecture above rules out. Four rules:

```python
for attempt in (1, 2):                      # 3. BOUNDED retry — a swallowed tap costs a repeat
    ui.tap_at(...)
    reading = read_barter_panel(...)        # 1. TAP, THEN READ BACK — verify the effect
    if _panel_signature(reading) != last_sig:
        break                               # 2. A SIGNATURE decides whether the screen moved
if _panel_signature(reading) == last_sig:
    return None                             # 4. TRISTATE: None is "the tap never landed",
                                            #    NOT False ("wrong good")
```

Rule 4 is the one that matters most and the one the market has nowhere: **"the action did not
happen" is a different answer from "the action happened and the result is no".** `_try`'s
docstring says it outright — *"None = the tap never landed, so this tile says NOTHING about
the village."* Every market failure above collapsed those two into one, and then reasoned
from the collapsed value.

The village pays for this and says why: *"a swallowed tap costs the tile nothing but a
repeat; the second tap meets a screen with no tip on it."* One extra tap against a lost
mission.

**Requirements this puts on the market's contexts** — these are not optional polish:

- every handler that taps a commit control returns a TRISTATE, and the activity must not
  treat `None` as a negative result;
- the effect is verified by comparing an observation before and after — a signature, a
  basket count, a cart state — never by the tap returning;
- one bounded re-tap on the same control before concluding anything;
- and the retry is NOT a loop. The handler taps, returns, and the next tick either sees a
  changed screen (done) or the same one (tap again) — the context is still `goods_staged`, so
  the same handler fires. The bound lives on the activity, keyed to the goal, as `_selects`
  and `_committed` already do. This is not a preference; it is the architecture: a retry loop
  holds control across a capture, and a dialog arriving mid-retry is tapped over rather than
  seen — which is FC-3 exactly.

## The line to keep: who caused the dialog

CLAUDE.md already draws it by cause, and the context model gives it a home:

- **Ours** (confirm / result / negotiation / quantity) → a CONTEXT of the market activity.
  Complete it; never dismiss it.
- **Unsolicited** (daily news, promo, announcement) → the dispatcher's obstruction layer,
  cleared before any activity is resolved.

A market context table must therefore contain only dialogs the market's own actions raise.
Anything else it sees is `MISS`, and handing back is correct.

## Migration — the case study

Small steps, each independently checkable, in this order:

1. **Write `brain/market_context.py`** and classify recorded frames with it. No behaviour
   change. Run it over the trace sessions from 2026-08-29/30 and check every frame lands
   somewhere sensible — this is where the vocabulary above gets corrected.
2. **Convert the READ path first** (`_read_owned_via_sell`, the page reads). Lowest risk: no
   taps, and it is where the `Im glad` misread happened.
3. **Convert the sell flow**, one context at a time, keeping `sell_goods` callable until the
   handlers cover it.
4. **Convert the buy flow** — `buy_to_goal`'s 47 rounds become 47 ticks, which also puts the
   ledger's "count what this session bought" on a per-tick footing.
5. **Delete the private dialog waiters** once no path reaches them.

Each step is measurable in the same way: the sub-loop count in the market path, and whether a
run still completes.

**The acceptance test is mechanical, and it is the architecture's own:** for every function
in the market path, *can control be inside it while the screen shows something it did not
expect?* The migration is done when the answer is no everywhere — not when the count is low,
and not when the loops are careful. `commit_via_positive_taps` was careful.

## Performance — one perceive per DECISION, not per action

The concern is real and measured. At Svear on 2026-08-30 a single barter round took **57
seconds** (round 1 at 00:49:08, round 2 at 00:50:05), and the time is not going where it
looks:

```
00:49:16  read_barter_panel   ┐
00:49:16  read_barter_panel   │  FOUR reads of the SAME panel
00:49:20  read_barter_panel   │  before a single tap
00:49:23  read_barter_panel   ┘
00:49:29  tap 'Exchange'
00:49:39  tap 'Ok'
00:49:48  cycle closed
00:49:59  read_barter_panel   ┐  two more after
00:50:05  read_barter_panel   ┘
```

**Six reads of one panel in one round.** `VillageActivity._panel()` has no caching — every
call runs a fresh capture + parse — and five call sites reach it per tick. One of them is a
capture-and-parse to print a log line:

```python
logger.info(f"[village] round {self._committed} committed "
            f"(amity {before} -> {_amity(self._panel())})")     # village.py:266
```

So three separable costs, and only the third is about tick granularity:

1. **Re-reading the same screen within one tick.** A per-frame-sharing violation
   (CLAUDE.md: "capture once per tick, pass the same Image"). Fix: `_panel()` caches for the
   tick's frame. Six reads → one. This is the biggest single win and it changes no control
   flow at all.
2. **Reading in order to LOG.** Free to delete; the value is already in hand or not needed.
3. **A perceive between every tap.** This is the one the pattern introduces, and the rule
   below bounds it.

### The rule

**A tick is one observation → one decision → the actions that decision entails.** Not one tap.

A perceive is required only when the next action's TARGET or VALIDITY depends on a previous
action's effect. Actions justified by the SAME observation may be executed together:

| batchable | why |
|---|---|
| stage five goods located in one grid read | tapping a tile does not move the others |
| tap Matchlock Gun and Iron in the same visit | both located in the read that chose them |
| type an amount into a dialog already on screen | the field was in the observation |

| NOT batchable | why |
|---|---|
| tap a tile, then type a quantity | the dialog's existence depends on the tap |
| tap Exchange, then confirm | the confirm dialog is the tap's effect |
| stage, commit, then stage again | committing RE-FLOWS the grid — every position is stale |

The boundary is the one Principle #5 already draws: *a loop may wait for its OWN effect; it
may never perform a sequence.* Batching does not wait at all — it spends an observation that
already justified every tap in the batch.

### What batching actually saves

Anti-cheat forbids bursts (≥3 taps in <1s), so batched taps still carry their jitter. The
saving is the PERCEIVE, not the taps:

```
one perceive (capture + OmniParser)   ~2-3 s
one tap (with anti-cheat jitter)      ~0.3-0.8 s
```

Staging five goods as five ticks costs ~5 perceives ≈ 10-15 s. As one decision it costs one
perceive plus five jittered taps ≈ 4-7 s, and the anti-cheat discipline is untouched.

### The market's own numbers, measured (2026-09-05, Madeira)

The Svear figures above are the village's. The market is worse, and in a different way — one
buy cycle, 110 units of Raisin, **87 seconds**:

```
12.7s  post-purchase market read          9.5s  find + tap Purchase
11.0s  refresh verify read                9.1s  confirm dialog
 6.3s  a SECOND full read 7s later,       7.9s  refresh icon → Replenish OK
       identical values                   6.9s  negotiation popup ("No")
 8.7s  find + tap goods tile              6.8s  result dialog
```

Three or four FULL-PAGE reads per cycle, each running OmniParser over the whole frame and
then OCR'ing every tile individually via `fill_missing_quantities` — at Madeira that is
Madeira Wine, Sugar, Keris, Sugar Cane, Wooden Statue and Shea Butter, when the order wanted
Raisin. Two of those reads returned IDENTICAL values seven seconds apart.

The user's framing is the rule for this work: *"we should not act blindly, but also should
not try to read the same frame again and again"* (2026-09-05). Both halves have cost a run —
acting blind produced the route-tab mis-sail the same evening, and re-reading produced this.

**Use the machinery that exists rather than adding a cache.** `vision/perceive_repository`
already models it: an `Observation` with a GENERATION, `current_if_valid()` (valid exactly
while nothing has acted), and `_derived[(generation, key)]` with `elements_if_ready()` so a
parse is made once per observation and reused. Routing market reads through it makes reuse
safe BY CONSTRUCTION; a TTL or a private cache would re-introduce the stale-frame bug on a
timer, which is the worse of the two failures.

## What must not regress

Guards earned live in September, all in `actions/buy_materials.py` and all POLICY the
handlers must carry rather than re-derive:

- **per-good, never the sum** — a surplus of one material must not cover another's shortfall;
- **`believed` is a LOWER BOUND** (`c753d6d`) — an unread purchase only ADDS, so a floor that
  clears the goal is met with certainty; testing `unknown` first produced a buy loop with no
  exit, six gems deep and heading for the 60-round cap;
- **a gem only buys a restock of something this market still owes us** (`2599067`) — and
  because a refresh is MARKET-WIDE, the good that justifies it need not be the one whose
  shelf emptied (Barcelona: Iron met, Matchlock short → refresh is right);
- **a shelf about to restock itself is not a refused shelf** (`0043cbb`);
- **the separator is whatever OCR saw** (`53431d9`) — `3.129` is 3,129;
- **`None` is not `[]`** — a leg that never saw the hold must refuse, not report success.

### What this means for the migration

Step 2 (the read path) is worth doing FIRST for performance as much as for correctness — the
caching fix above lives there, and it is measurable on recorded frames with no live run. The
`buy_to_goal` worry in the open questions below is then narrower than it looked: 47 rounds do
not become 47 perceives, because a round's staging taps are one decision.

## Open questions

- **A round is many ticks now** — partly answered by the batching rule above: a round's
  staging taps are ONE decision, so 47 rounds are 47 perceives, not 47 x N. Still open is
  whether 47 perceives is itself too many, and whether the restock/refresh cycle inside a
  round can be folded in.
- **Where does the ledger live?** `market_ledger` accumulates what was bought this session.
  As contexts, the natural home is the `result_dialog` handler — but the ledger outlives any
  one dialog, so who owns it?
  **Sharpened by evidence, 2026-09-05.** The ledger got it wrong in BOTH directions in one
  evening: FC-2 recorded a purchase that never happened (no result dialog, no goods, an entry
  anyway), and the Madeira runaway under-counted so badly that the tile read 1,870 while the
  ledger believed 1,582 and kept buying. So the answer is not only WHERE it lives but WHAT
  MAY WRITE TO IT: **only a `result_dialog` may add to the ledger**, which makes FC-2
  unreachable, and a reading that disagrees with it must be reconciled rather than ignored.
- **Is `negotiation` one context or several?** It has its own outcomes and may need a policy
  the goal carries, not the context.
- **What replaces the scroll loop?** Selling re-flows the grid, so "sell page → scroll →
  repeat" is a sequence. As contexts it becomes `sell_page` with a `scrolled` marker — which
  is state the activity holds, and state is what contexts are supposed to avoid.
- **How does a handler bound its own retries without holding state?** The dropped-tap rules
  above want "one bounded re-tap", but a context is meant to be stateless. The village's
  answer is an attempt counter on the ACTIVITY keyed to the goal (`_selects`, `_goal_key`),
  reset when the goal changes — the same shape as `_committed`. Adopting that keeps the
  contexts stateless and puts the bound where the goal's lifetime already is.
- **Does `Load All` replace per-tile staging on the sell page?** It is on screen in FC-4 and
  would have made that failure impossible. If the goal is "sell everything except a keep
  list", one tap plus removals may be strictly better than N tile taps — and it is far fewer
  chances for a swallowed tap.
