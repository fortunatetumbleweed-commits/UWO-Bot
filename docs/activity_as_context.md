# The activity as a CONTEXT — eliminating flow sub-loops

**Status:** design, 2026-08-27. Branch `context_scoped_perception`.
**Supersedes the sub-loop halves of:** `buy_to_goal`, `sell_goods`, `sell_down_to`,
`run_barter_phase`, `read_village_barter_remote`.
**Builds on:** `docs/one_loop_task_drives_state.md` (the task owns the sequence),
CLAUDE.md *Data has an OWNER, and dies with it*, and *Never store a conclusion*.

---

## 1. The problem, measured

Every long-running action in this bot is a private loop that acts, looks, and acts again
without ever handing back:

| function | lines | loops |
|---|---:|---:|
| `actions/buy_materials.py::buy_to_goal` | 412 | 3 |
| `actions/sell_goods.py::sell_down_to` | 194 | 3 |
| `actions/village_check.py::read_village_barter_remote` | 167 | 1 |
| `actions/sell_goods.py::sell_goods` | 156 | 2 |
| `brain/barter_mission.py::run_barter_phase` | 33 | 1 |
| **total** | **~960** | **10** |

A loop that only waits for its own effect is fine. These do not: each one performs a
SEQUENCE of world-changing actions — buy, refresh, buy again; commit, commit, commit — and
between them the world can move in ways the loop has no vocabulary for.

So each has grown a private copy of the dispatcher. In `buy_to_goal` alone:

```
 5x  clear blockers          -> Dispatcher.tick already has `unblock`
 4x  re-open the Purchase grid \
 3x  re-read a grid that came back empty  > Dispatcher.tick already perceives
 2x  re-find a tile that moved /
 6x  early return            -> ActivityResult(UNRECOGNISED) already exists
 5x  blue-gem refresh        -> the only one of these that is actual WORK
```

That is not a cost argument against handing back. `buy_to_goal` already pays a full
`capture + OmniParser` per round via `_read_grid`, so moving the perceive up a level does
not add one — it *relocates* the one already happening to the layer that can act on it.

### What it cost, live 2026-08-27

- **The barter crash.** The day's rounds ran out, the game closed the barter submenu, the
  Exchange tap landed on the village menu behind it. `run_barter_phase` has no state for
  "the panel is gone", so it fell through to the only branch left — *the panel must be
  stale* — and called a refresh path that had never once executed. `TypeError`, run over.
- **The retired buy order.** `Hold(orders={'Iron': 506, 'Matchlock Gun': 253})` was handed
  to the arrival cinematic, whose one action is a tap. It returned `FINISHED`, the task
  runner read that as the goal being done, and the fleet sailed 95 Matchlock short.
- **The misdiagnosed grey Exchange.** A dead button was reported as "the day's rounds are
  spent" when a round remained and a material was at 0.

Each is the same shape: **a world change the loop could not name, resolved by guessing.**

---

## 2. The design

**One dispatcher. One perceive. The activity becomes a CONTEXT.**

A context is a *prior*: the bot assumes it is still in the market, or still in the village,
and perception is asked a scoped question — *which of the market's screens is this?* —
rather than the open one — *which of the forty screens is this?*

```
    perceive(context="market")
        -> a named market-local state   -> handle it here
        -> MISS                         -> full perceive -> a new context
```

The context is what keeps an activity meaningful once its flow loop is gone. Without it,
handing back after every transaction flattens everything into one undifferentiated pile of
states. With it, the activity is a **boundary**: a declared set of local screens plus an
exit condition.

`SERVES` already declares which OUTER states an activity claims. A context adds
`CONTEXT_STATES`: which screens it owns *inside* them.

### 2.1 The rule that makes it safe

> **The context must be POSITIVELY confirmed, never merely un-contradicted.**

`perceive(context=…)` must return a *named* context-local state, or it is a miss. "Nothing
proved we left" is not a match — that is a stored conclusion, and it is the exact failure
mode behind every bug in section 1.4 of CLAUDE.md. A context held on absence of evidence
would recreate the Placement Setting detour (17 ticks on a screen nothing could name) and
the cancelled Svear voyage (a stale destination outliving the world it was chosen in).

This also makes the boundary testable: enumerate, per context, the states that count as a
match. Everything else is an exit by construction.

### 2.2 Context lifetime IS cache lifetime

The context is the `PLACE`/`BUILDING`-owned datum from the ownership rule. Its lifetime is
the activity's lifetime, and **a miss is exactly the moment its cached data goes bad** —
the cart, the open tab, the selected tile, the shelf.

`brain/owned_state.py::observe()` already drops `PLACE`/`BUILDING`/`PANEL` on a world
change. Context-switch and cache-invalidation are the same event, so half of this is
already implemented; the change is to trigger it on a context miss rather than only on an
outer state change.

### 2.3 Where the tiers land

The codebase already cascades cheap-to-expensive (family CNN -> signatures -> Qwen). The
context is the new cheapest tier:

```
    context-scoped read   (one region: the sub-menu title, the panel, the dialog)
    full classify_nav_state   (on entry, and on a miss)
    Qwen                      (as today)
```

The scoped read must stay cheap or the economy is lost. It is mostly written already —
`ui.active_submenu`, `ui.on_submenu`, `buy_materials._on_sell_tab`,
`barter_reader.read_barter_panel`, `vision.overlay.detect_overlay`,
`buy_materials._find_purchase_menu`. Today these are called ad hoc from inside loops; the
change is to make them **one classifier that returns a context-local state**, the way
`classify_nav_state` does for the world.

---

## 3. The three obligations

Every context owes exactly three things. Nothing else belongs in an activity.

### ENTER — establish the context, do not assume it
Arriving is a transition the OUTER dispatcher performs (`ENTER_BUILDING`). The activity's
first act is to confirm it is where it thinks it is, by a positive read. It never navigates
to get there; if it is not there, that is a miss, and the outer layer routes.

### HANDLE — one action per context state
For each `CONTEXT_STATES` entry, one handler that performs ONE action and returns. Not a
loop. The next tick re-perceives and dispatches again — possibly to the same handler, which
is how repetition happens without a loop owning it.

**The indivisible unit is a transaction, not a tap.** A purchase raises a result dialog, and
by the dialog rule a dialog raised by the bot's own action must be COMPLETED, never
dismissed. So "buy and clear its result dialog" is one action; "tap Purchase" is not.

### EXIT — gracefully, on a miss
No context state matched. The activity returns `UNRECOGNISED` — the existing
`ActivityResult` contract, unchanged — and the outer dispatcher perceives and routes. The
activity does not decide where to go next, and never presses Back to make the screen match
its expectation.

A screen that genuinely IS the market but has never been modelled will also miss, and the
bot will leave a place it was legitimately in. That is the right trade: leaving is cheap and
safe (Home / back, already built in `actions/screen_exit.py`), and the alternative is
staying on the strength of a belief. It also makes the miss rate a useful metric — it is the
*screens we have not modelled yet* rate.

---

## 4. Use case: the barter

### 4.1 Today

```
village.work(goal)
  -> _barter(goal)
     -> _loop(goal)
        -> run_barter_phase(...)          # for _ in range(target): up to 20 rounds
             read_state_fn()              # localized perception INSIDE the loop
             commit_fn()                  # -> barter_commit_verified
             read_state_fn()              # again, for overflow
```

The outer dispatcher sees `village.work()` **once** and gets a result after all rounds. It
never perceives between them, while every round changes the hold, the amity, the remaining
rounds — and can close the submenu.

`max_rounds: int = 20` is an invented bound sitting on top of the real one (the day's
rounds), which is a symptom: the loop cannot see the true limit, so it carries a fake one.

### 4.2 As a context

```
CONTEXT_STATES (village):
    village_top_menu        the left menu: barter, explore, gifting, loot, recruit
    barter_panel_no_good    panel open, nothing selected
    barter_panel_ready      panel open, our good selected, Exchange live
    barter_panel_blocked    panel open, Exchange GREY -> a material at 0, or amity too low
    exchange_confirm        the "Barter Calculations" dialog raised by our own tap
    barter_result           the result dialog that proves the round happened
    overflow_prompt         the hold overflowed and must be jettisoned
```

One handler each, one action each:

| state | action | then |
|---|---|---|
| `village_top_menu` | open Barter | finish |
| `barter_panel_no_good` | select the good | finish |
| `barter_panel_ready` | tap Exchange | finish |
| `exchange_confirm` | OK — complete our own transaction | finish |
| `barter_result` | OK, record the round | finish |
| `barter_panel_blocked` | nothing — report which material is short | finish |
| `overflow_prompt` | jettison | finish |

The barter becomes: **commit -> perceive-in-context -> commit**. Rounds are counted by the
task runner, which already tracks mission progress, against the real limit.

### 4.3 The failure that motivated this

When the day's rounds ran out the game closed the submenu. Under this design:

```
perceive(context="village") -> village_top_menu        # a NAMED state, still in context
```

The context still matches — we never left the village — so the bot does not exit. The
handler asks the task runner, which knows the barter is done, and the mission moves on.
No stale-panel guess, no crash, no `refresh_fn` with the wrong signature.

Note this is precisely why "activity = context" beats "hand back after every round": the
outer state is `village` before AND after the submenu closes, so the OUTER perceive learns
nothing. Only a context-scoped read can tell those two screens apart.

### 4.4 The three endings, named

The screen distinguishes them — nothing has to be inferred (user, 2026-08-27):

| screen | meaning |
|---|---|
| panel open, Exchange GREY | rounds REMAIN; a material is short or amity too low |
| **submenu CLOSED** | the game shut it: the day's rounds are spent — completion |
| panel open, tap did nothing | genuinely stale; reopen once and retry |

---

## 5. Use case: the market

### 5.1 Today

`buy_to_goal` (412 lines, 3 loops) buys, detects a sold-out shelf, spends a blue gem,
re-reads, and buys again — seven such cycles at Bordeaux on 2026-08-27, all inside ONE
`market.work()` call. `sell_goods` and `sell_down_to` add five more loops.

### 5.2 As a context

```
CONTEXT_STATES (market):
    market_landing        the "Market Owner" greeting page
    purchase_grid         the buy grid
    sell_grid             the sell grid (also the cargo hold — memory: right panel is both)
    quantity_dialog       Trade Goods Info: the denominator IS what we hold
    restock_confirm       Replenish-Stock, raised by our own ↻ tap
    txn_result            the result dialog that proves a buy/sell happened
    grid_obscured         a promo/announcement over the grid
```

| state | action | then |
|---|---|---|
| `market_landing` | open Purchase (or Sell) | finish |
| `purchase_grid` | goal unmet? buy the buyable — else finish the goal | finish |
| `purchase_grid` + shelf greyed | tap ↻ | finish |
| `restock_confirm` | OK — complete our own transaction | finish |
| `sell_grid` | stage / trim per the goal | finish |
| `quantity_dialog` | read the denominator, type, Load | finish |
| `txn_result` | OK, credit the ledger | finish |
| `grid_obscured` | *no handler* — this is the dispatcher's `unblock` | — |

`grid_obscured` earns its place in the list only to be **deleted**: it is the five
`clear_blockers` calls, and the outer dispatcher already clears obstructions before
dispatching. That is the single largest deletion in the change.

### 5.3 What the market context does NOT own

`transient` over the market, an unnameable chromed screen, arriving at `sea` — none of these
are market states. They are misses, and they already have homes: `TransientActivity`,
`UnrecognizedChromedActivity`, the outer routing.

---

## 6. What gets deleted

- `brain/barter_mission.py::run_barter_phase` — entirely
- `buy_to_goal`'s round loop and its blocker-clearing, grid re-reads, and tile re-finding
- `sell_goods` / `sell_down_to`'s page loops
- `max_rounds`, `max_attempts` and the other invented bounds that exist because a loop
  cannot see the real limit
- the "the panel must be stale" inference — staleness becomes one named state among three,
  reached only when the other two do not match

Rough estimate: **~960 lines of loop and compensation, against perhaps 250 lines of
handlers and one scoped classifier.**

---

## 7. Open questions

1. **Frame sharing.** One capture per outer tick passed down, or a fresh capture per
   handler? Sharing is cheaper but risks acting on a stale frame — and a transaction's
   result dialog is exactly where the screen changes underneath.
2. **Miss cost.** A miss pays a full `classify_nav_state`. If the scoped read is
   over-strict, misses become frequent and the economy inverts. Instrument the miss rate
   from the first live run.
3. **Where the scoped classifier lives.** `brain/perceive.py` beside `classify_nav_state`,
   or per-activity? One module is the "one canonical implementation" answer, but the state
   sets are activity-specific.
4. **Migration order.** Village first: fewer states, and it is the one that crashed. Market
   second, where most of the deletion is.

---

## 8. Why this is the same rule as everything else

The bot's recurring failure is a belief that outlives its evidence: a destination chosen
before departure, a recipe cached from a partial read, a tab assumed rather than checked, a
learned icon position saved from the wrong panel.

A flow sub-loop is that failure given a place to live. It holds a belief about the screen
across actions that change the screen, and it has no way to be told it is wrong. The context
makes the belief **cheap to falsify** — one scoped read per tick — and gives being wrong a
name: a miss.

---

## 9. The Android mapping — and the one place it breaks

The design above is Android's activity model, and borrowing it deliberately is worth doing:
the shapes we keep rediscovering (context, intent, finish, dialog-inside-activity) are the
ones Android settled decades ago.

### 9.1 Two dispatch mechanisms, not one

Android dispatches at two very different granularities, and **conflating them is our bug**:

| | Android | ours |
|---|---|---|
| **Intent dispatch** | `ActivityManager` resolves an Intent to an Activity via intent filters. Coarse, rare, changes what is running. | the outer dispatcher: `ENTER_BUILDING`, depart, open the world map |
| **Input dispatch** | `InputDispatcher` -> focused window -> view tree -> the activity's listeners. Fine-grained, constant, changes nothing about what is running. | perceive -> context -> the activity's own handlers |

The essential property of input dispatch is that the **transport and routing are central
while the interpretation is local**. `InputDispatcher` decides WHICH window gets the event;
it has no opinion about what the event means. The view tree hit-tests; the activity's
listeners decide.

So: one perceive (the event source), one dispatcher (the router), and interpretation inside
the activity. A dialog opening, a tab switching, a barter round committing — all of these
are input-dispatch events. **None of them is an intent.**

### 9.2 Most interactions produce no intent — and that is the ratio to aim for

In an Android app the overwhelming majority of taps never reach `ActivityManager`. Buttons,
dialogs, list scrolls, form entry: the activity stays, the screen changes, listeners run.

That gives a diagnostic. **Count intents per tick:**

- `buy_to_goal` today emits ZERO intents across seven blue-gem refresh cycles and does
  everything internally — the loop is an activity that never yields.
- A naive "hand back after every transaction" design emits one intent per action — an
  activity that yields for things that are not transitions.
- Correct is: **an intent only when the world changes.** Buying, refreshing, staging,
  confirming a dialog, committing a barter round — none of these leave the market or the
  village, so none is an intent.

If a context is emitting intents often, its boundary is drawn in the wrong place.

### 9.3 A dialog does not end the activity

An Android `Dialog` is a window ON TOP of a still-running activity. The activity is not
destroyed; it handles the dialog's buttons through its own listeners.

We had this backwards. The quantity dialog, the Replenish-Stock confirm and the barter
result dialog were each handled ad hoc from inside a loop, and a dialog we could not place
became an exit or a crash. Under the context model they are ordinary context states with
handlers — which is also exactly the existing rule that **a dialog raised by the bot's own
action must be COMPLETED, never dismissed** (CLAUDE.md). Android enforces that structurally:
your own dialog belongs to your activity, so of course you handle it.

### 9.4 `finish()` is an intent too — but our back stack is OBSERVED, not maintained

`finish()` does not launch anything; it ends the current activity and the framework reveals
what was beneath it. That maps onto our `ActivityResult(FINISHED | UNRECOGNISED)` exactly.

But here the analogy needs care. **Android's back stack is state the framework HOLDS. Ours
is physical and held by the game.** Leaving the market reveals the port overworld because
that is where the fleet is standing — not because we remembered a stack.

So we must never model a back stack. `finish()` -> perceive -> whatever is actually there.
A predicted "what was before it" would be a stored conclusion, and the last time we kept one
of those the fleet sailed back to Barcelona mid-voyage.

### 9.5 Intent filters are already built

Android resolves an implicit Intent by matching it against registered filters. We built that
today without naming it:

```python
MarketActivity.SERVES = ("building:market", "sub_menu:market", "sub_menu:purchase", "sub_menu:sell")
MarketActivity.GOALS  = (Hold, FreeHold, TrimHold)
```

`SERVES` is a filter on the STATE; `GOALS` is a filter on the INTENT. `to_intent()` is the
resolver: given `Hold(...)` and a state that no market serves, it returns
`ENTER_BUILDING(market)` — an explicit intent to start the activity that registered for it.
The goal-aware dispatch added on 2026-08-27 is the missing half: refusing to deliver an
intent to an activity whose filter does not match it.

`CONTEXT_STATES` completes the picture: a filter on the SCREENS an activity owns while it is
running.

### 9.6 `onResume` — re-read what you cannot trust

Android calls `onResume()` when an activity comes back to the foreground, and the convention
is to re-read anything that may have changed while you were away. That is the ENTER
obligation in section 3, and it is the same rule as *data has an OWNER*: on re-entry,
`PANEL`-scoped data (the cart, the selected tile, the open tab) is gone and must be re-read,
while `FLEET` data survives.

### 9.7 Where the analogy BREAKS — and why it is the important part

**Android always knows which activity is on top. We never do.**

The framework holds the activity stack as authoritative state. `getRunningTasks()` is a
lookup, not a guess. For us, "which activity is running" is itself a PERCEPTION, inferred
from pixels, and it can be wrong.

Three consequences follow, and they are the reason section 2.1 exists:

1. **The context is a hypothesis, not a fact.** It must be re-confirmed every tick by a
   positive read, and confirming must be cheap enough to do that.
2. **There is always a MISS case.** Android has no equivalent — an event cannot fail to
   find a window. Ours can, and the miss is not an error: it is the normal signal that the
   hypothesis expired.
3. **We cannot be told about transitions, only observe them.** Android delivers
   `onPause`/`onStop`. Nothing tells us the barter submenu closed; we find out by looking.
   So every handler must return to the loop promptly enough for the next look to be timely
   — which is another way of saying: no sub-loops.

This asymmetry is why the borrowed model needs the one rule Android does not: **positively
confirm the context, never merely fail to contradict it.** Android can afford to trust the
stack. We are always one unobserved transition away from acting in the wrong world, and the
only defence is looking again, cheaply, every tick.

### 9.8 The asymmetry named plainly

Android has full control of the screen because it AUTHORS it. Nothing appears that the
framework did not put there, so the framework can hold the truth and hand it to you.

We have no control at all. The game decides what is on screen, and it can interrupt with a
daily-news popup, an idle lock, a promotion, an arrival cinematic, a greeting page or a
settings screen we have never modelled — at any moment, without telling us. Our entire job
is to **observe and work out what the game has done.**

That is the whole reason the borrowed model needs adapting rather than copying:

- Android *knows* its activity stack; we *hypothesise* our context and re-confirm it.
- Android *delivers* `onPause`; we *notice* that the submenu closed.
- Android's back stack is *held*; ours is *revealed*, and must be looked at.
- Android has no MISS; ours is the normal, expected signal that a hypothesis expired.

What survives the adaptation intact is the division of labour, and it is the principle worth
keeping:

> **Centralize the observation and the routing; localize the interpretation.**

Recorded as Guiding Principle #1 in CLAUDE.md, under #0 — *we do not control the game, we
observe it* — from which it, and every other rule in this project, follows.

---

## 10. A third sub-loop, found live: `_await_route_arrival`

Live 2026-08-28, arriving at London on the `san to london` route. The screen was a
COMPOSITE nobody had modelled:

  * the **idle lock** underneath — titled "London", "Slide up to unlock";
  * the **daily news panel** on top of it, with its own close X.

Two interruptions stacked. And the idle lock NAMES THE PORT — so the fleet had arrived,
and the evidence was on screen — while `_await_route_arrival` reported *"still sailing"*.

    def _await_route_arrival(start, route_name, timeout_s=4 * 3600):
        while _t.monotonic() < deadline:
            _t.sleep(wait * random.uniform(0.9, 1.1))
            port = _current_port(tries=1)
            if port: return {"ok": True, ...}
            ...
            logger.info("still sailing ...")

It is the same shape as the other two, with one addition that makes it worse: **it waits
for something that is not its own effect.** Arrival is the world's doing, not this
function's, so by CLAUDE.md's rule this loop should not exist at all. And because nothing
in it clears an interruption, the idle lock it is blocked by will still be there on every
one of its polls — for the full four hours.

Under the context model there is no wait loop. Sailing is a state; each tick perceives;
`IdleLockActivity` swipes; the news is an interruptor the dispatcher already clears; the
next perceive reads `port_overworld / London` and the task advances. The composite needs no
special handling, because each layer is removed by whatever owns it.

Add to the deletion list in section 6:

  * `brain/barter_mission_live.py::_await_route_arrival` — 4-hour poll loop, 1 loop

which brings the total to roughly **six sub-loops, ~1,000 lines**.

### 10.1 The machinery already existed — the loop routed around it

`idle_lock` is a registered state with an activity, and it WORKS: `perceive` named it 15
times across 2026-08-28's runs, and the morning's launch cleared one on its own before
reading the fleet.

    registry["idle_lock"]                   -> IdleLockActivity
    registry["transient"]                   -> TransientActivity
    registry["unrecognized_chromed_screen"] -> UnrecognizedChromedActivity

So the thing that gets past the London screen was present, proven, and one swipe away.
`_await_route_arrival` never reaches it, because the dispatcher and its registry only run
inside `run_goal` and the wait loop bypasses them:

    while _t.monotonic() < deadline:      # 4 hours
        _t.sleep(...)
        port = _current_port(tries=1)     # no perceive, no dispatch, no activities

This reframes the whole argument. **The sub-loops do not merely LACK recovery — they route
around recovery that already exists.** `buy_to_goal` rebuilt blocker-clearing five times
over while `Dispatcher.tick`'s `unblock` sat unused; `run_barter_phase` guessed "stale
panel" while the village had no state for a closed submenu; `_await_route_arrival` would
have polled for four hours at a lock one registered activity dismisses.

So the case for this change is not "add a mechanism". It is **stop bypassing the one that
works** — and the deletions in section 6 are most of the change precisely because the
replacement is already written.

### 10.2 The sub-loops do not PERCEIVE — they READ

Two separate mechanisms clear things, and only one of them was ever missing:

| mechanism | handles | where it runs |
|---|---|---|
| `perceive.dismiss_interruptors` | IN-GAME popups: daily news, notices, promos | **Pass 1 of every `perceive`** |
| `unexpected_dialog.clear_blockers` | NON-GAME screens: the OS lock/screensaver, store popups | called explicitly |

`_perceive_uncached` opens with `frame = dismiss_interruptors(frame)`, so the daily news is
dismissed on EVERY perceive, everywhere — driven by the FSM registry's 42 interruptors. It
worked all day: `[perceive] Interruptor detected: 'daily_news' — dismissing`.

Which locates the London failure exactly. `_await_route_arrival` never calls `perceive()`.
It calls `_current_port(tries=1)` and `read_sea_hud()` — it READS FIELDS. So it got neither
pass, and would have polled for four hours past a popup that one ordinary perceive dismisses.

**That is the general statement of the problem.** Everything this bot knows how to clear is
attached to PERCEIVING; a loop that only reads the fields it cares about receives none of
it. The context model does not add clearing — it makes every tick a perceive, which is where
the clearing already lives.

---

## 11. The sea is an activity, and sailing has no loop

**Sailing a route is not different from sailing to a port or a village** (user, 2026-08-28).
The only thing that varies is the DESTINATION — and that can be either: the routes used so
far all end at ports, but a route can target a village when it is far enough to be worth one.
So there is one operation with a parameter, not three operations.

### 11.1 What exists today

Three implementations, two of them loops, none of them in the dispatcher:

| path | driver | perceives? |
|---|---|---|
| `sail_to_port` / `sail_to_village` | `drive_sail_to` — `while not complete: goal.tick()` | YES, every tick |
| `sail_route` | `_await_route_arrival` — `while monotonic() < deadline` | **NO** — reads fields |

And there is **no `sea` activity at all**:

    registered states: building:harbor, building:market, idle_lock, sub_menu:*,
                       transient, unrecognized_chromed_screen, village
    sea -> None

`"no activity for state 'sea'"` appears ZERO times in the 2026-08-28 logs — not because the
sea is handled, but because the dispatcher never meets it. Every sailing leg runs in a loop
outside it.

### 11.2 What it should be

The sea is a state like any other, and it gets an activity. Then:

    depart          -> an INTENT (the one thing that changes the world)
    sailing         -> the sea context: tick, perceive, do nothing much
    arrival         -> the sea context stops matching -> MISS -> the dispatcher routes

**Arrival is not an event to WAIT for; it is a context change to NOTICE.** Nothing needs to
ask "have I arrived?" — the next perceive says `port_overworld` or `village` instead of
`sea`, which is a miss, which is already how a context ends. That is the same mechanism that
ends a barter when the submenu closes.

This removes both loops. `drive_sail_to`'s is the more defensible of the two — it does
perceive each tick — but it is still a private loop owning a sequence, and it is why the
sea never reaches the dispatcher.

### 11.2a CORRECTION — the sea needs no activity, because the PORT has none either

    port_overworld  -> None
    world_map       -> None
    sea             -> None
    village         -> VillageActivity
    building:market -> MarketActivity

The distinction is not sea-vs-port. It is **places you do WORK** (village, market, sub-menus
— these have activities) against **places you PASS THROUGH** (port overworld, world map, sea
— these have none, and should not).

The port overworld proves the pattern works: `no activity for state 'port_overworld'` fires
24 times in a day and is not an error. The dispatcher asks the task runner for a goal,
`to_intent` turns it into `ENTER_BUILDING`, and the bot moves. A state with no activity is
exactly right where there is nothing to DO, only somewhere to GO.

So the sea does not need an activity. It needs what the port already has: **to be reached
through the dispatcher at all.**

    port_overworld:  dispatcher ticks -> no activity -> ask for a goal -> intent -> move
    sea:             never reaches the dispatcher; a private loop owns the whole voyage

`port_overworld` appears 24 times because the dispatcher genuinely meets it. `sea` appears
ZERO times because `drive_sail_to` and `_await_route_arrival` hold the fleet for the entire
leg and never hand back.

The one genuinely sea-specific need — the supply watch — is a GOAL the task runner holds,
not an activity: the same shape as `Hold(orders=...)`, and answerable from the HUD the
dispatcher already perceives.

### 11.3 What a sea TICK actually does (if an activity is wanted at all)

Very little, which is the point. Its repertoire is a monitor, not a procedure:

  * read the HUD (supply days, ETA)
  * enforce the supply floor — a village leg carries a round-trip reserve
  * otherwise: nothing. Report WORKING and let the next tick look again.

An activity whose action is usually "nothing" is not a defect; `IdleLockActivity` does one
swipe and `TransientActivity` one tap. What matters is that the tick still PERCEIVES, which
is where interruptor dismissal lives (section 10.2) — the thing `_await_route_arrival` never
did, and the reason it would have polled four hours past a daily-news popup at London.

### 11.4 The cadence moves, it does not disappear

`_await_route_arrival` derives its sleep from supply (`supply_checkback_seconds`) so a fleet
with five days aboard is re-checked in ~6 minutes rather than every few seconds. That
reasoning is sound and must survive: it becomes the **tick delay for the sea context**, set
by the dispatcher, rather than a `sleep` inside a loop. Pacing is a property of the state,
not of a procedure.

Add to the deletion list:

  * `drive_sail_to`'s while-loop
  * `_await_route_arrival` entirely
  * whatever of `sail_to_port` / `sail_to_village` / `sail_route` collapses once the
    destination is a parameter rather than three call paths

---

## 12. FUTURE WORK — every whole-screen world is an activity, and TWO of them are containers

Design direction, not yet built (user, 2026-08-28). It supersedes the framing in 11.2a,
which drew the line at "places you do work vs places you pass through". The better rule is
simpler.

### 12.1 The rule

> **A whole-screen world is an activity.**

That covers everything, with no case analysis: `sea`, `port_overworld`, `world_map`,
`market`, `village`, the other buildings, and every chromed screen we have not modelled yet
— the Fleet Management / Placement Setting screen included. There is no second category of
"transit states that get none". A world fills the screen; a world is an activity.

This is why 11.2a was wrong. It read `port_overworld -> None` as a design decision when it
is just an absence: the port overworld has no activity because nobody has written one, and
the dispatcher falls through to "ask for a goal" — which happens to work for entering a
building and would work no better for anything else.

### 12.2 Sea and port are CONTAINERS

`sea` and `port_overworld` are not two more worlds among many. They are the **two roots**:

  * every chromed screen is entered FROM one of them, directly or through others;
  * pressing back repeatedly from any chromed screen ends at one of them;
  * so they always sit at the bottom of the stack, and nothing sits under them.

That matches the ownership model already in CLAUDE.md, where the port world and the sea
world are the CONTAINERS that own everything shorter-lived (`PLACE` > `BUILDING` > `PANEL`).
The same two things are the roots of navigation and the roots of data lifetime, which is a
sign the decomposition is the right one.

**What it buys: a termination guarantee.** `exit_current_screen` and the
`unrecognized_chromed_screen` activity both work by leaving — Home, or back, repeatedly. Today
that is bounded by a counter and a hope. Under this rule it is bounded by the STRUCTURE:
backing out of any chromed screen terminates at sea or port, because there is nowhere else to
land. The escape stops being a heuristic and becomes provable.

Note the asymmetry with Android that section 9.7 already names: Android's back stack is state
the framework HOLDS. Ours is physical, held by the game — so this is not a stack we maintain,
it is a shape the world has. We still never predict what is beneath; we perceive it. The rule
tells us where the descent ENDS, not what each step reveals.

### 12.3 The idle lock is the exception that fits

The idle lock is not entered from anywhere — the game imposes it over whatever was on screen,
so it has no parent in the stack. But it resolves INTO the shape: when its swipe finishes,
the bot is either

  * already at one of the two containers, or
  * on a chromed screen that backs into one of them.

So it is an exception to how it is ENTERED, never to where it LEADS. That is consistent with
what `IdleLockActivity` already does — one action, destination unreported, because the
destination is whatever the next perceive says (section 3, EXIT).

### 12.4 What this would change

  * `port_overworld`, `sea` and `world_map` get activities, so the dispatcher meets them.
  * Both sailing loops go (section 11), because the sea is somewhere the dispatcher ticks.
  * The escape's bound becomes structural rather than a retry count.
  * The Fleet Management screen, and every other unmodelled chromed screen, has an obvious
    home: it is an activity nobody has written yet, and until someone does,
    `unrecognized_chromed_screen` handles it — which is exactly what it is for.

---

## 13. A navigation loop holding a system-level situation — 2026-08-28, fresh start

The strongest evidence for the whole change, because here the sub-loop did not merely fail
to recover: **it manufactured a false answer.**

A fresh run began with the phone locked. The lock cleared, `clear_blockers` dismissed the
daily news, and an event/perk popup was left covering the screen. A fresh run has NO prior
belief about where the fleet is, and the popup makes sea and port indistinguishable. So the
bot could not know its position — which is the correct thing to not know.

What it did with that:

    [open_world_map] loc='transient' not port/sea — waiting to re-perceive (attempt 8/10)
    _is_on_overworld: SceneModel uncertain (family=chromed kind=sub_menu:perk conf=medium)
    read_port_name:   raw OCR 'Season' did not match any known port (similarity 0.56)
    _is_on_overworld: Overworld confirmed: port name 'Season' visible
    [open_world_map] failed to open world map after retries

Three failures, in order of increasing seriousness:

1. **It waited for something that would never happen.** `TransientActivity` taps a
   full-screen notice away. It was reached **0 times** — `open_world_map` is called from
   `barter_command`'s preamble, which never ticks through the dispatcher.
2. **It read THROUGH the popup and believed the result.** "Season" came from the
   *Investment Season 7* banner, and `_is_on_overworld` accepted it as a port name and
   declared the overworld confirmed — overriding a scene model that had already said
   `sub_menu:perk`. That is Guiding Principle #3 (text in the port-name position is assumed
   to BE a port name) and #2 (the conclusion outlives the look) in one line.
3. **It gave up after ten attempts** and ended the run.

### 13.1 A navigation primitive was left holding a system-level situation

`open_world_map` knows how to open a map. It was handed: an OS lock screen, a daily-news
popup, and an in-game perk banner. It has no vocabulary for any of them, and its three
available responses — wait, guess, give up — are all wrong.

Nothing about that is fixable inside `open_world_map`. Teaching it about perk popups would
add a fourth special case to a function whose job is one tap, and the next unmodelled screen
would need a fifth.

### 13.2 The startup protocol the dispatcher should own (user, 2026-08-28)

After an unlock the situation is UNKNOWN, and establishing it is the dispatcher's job, not
any primitive's:

    loop:
        perceive                       (Pass 1 already dismisses named interruptors)
        is the state one we recognise and can work in?  -> stop
        otherwise pick the activity for it — transient / idle_lock /
        unrecognized_chromed — and let it clear ONE layer
        perceive again; there may be another layer beneath

Only when a workable state is reached does the dispatcher **take a work order from the
company task runner**. Today the order comes first and the preamble starts navigating before
position is established — which is how a map-opener came to be holding a lock screen.

Note this needs no new machinery. It is the ordinary tick with no goal yet, and the
screen-clearing activities already exist and already work. What is missing is that the
BOOTSTRAP goes through the dispatcher at all.

---

## 14. Intents open activities, and the purpose rides in the extras

**The universal mechanism for an activity transition is an INTENT** (user, 2026-08-28). Not
a function call that navigates, and not an activity reaching for the screen it wants — the
dispatcher dispatches an intent, and the intent carries WHAT FOR.

The machinery is already there and already says so:

    @dataclass
    class Intent:
        """`extras` carries the PURPOSE, as an Android intent does — the world map is
        entered to set sail or to make a remote check, and behaves differently for each,
        so the purpose is part of the request rather than something the activity has to
        infer."""
        name: str
        extras: Mapping[str, Any] = field(default_factory=dict)

`extras` is Android's generic container: whatever the destination activity needs, without a
new intent type per purpose. So there is ONE `OPEN_WORLD_MAP`, and the goal travels in it.

### 14.1 What the world map is actually for

Every caller of `open_world_map` says on the next line what it went there to do — the intent
already exists in the code, it just is not carried:

    village_check        read a village's trade list remotely (Village Info -> Base/Barter)
    route_execution      select a saved route and Move
    commit_departure     navigate to a destination and depart
    run_event_sell       read the Trade Event Schedule
    nav_step / agent     just get there — one navigation step

Plus, on the same screen and in the code already: the Trade Event flow taps a city pin ->
**Location Info** -> Move, which is a DIFFERENT way to depart than the destination panel;
and **Invest**, **My Location**, and city/port info reading (Milestone 4 lists city-info as
open). Future, per the user: **find a powerful enemy**.

    ChooseDestination(where, kind)   pick a port / village / route and commit   [built]
    RemoteCheck(village)             read a village's trade list from afar
    ReadEventSchedule()              the Trade Event Schedule dialog
    MoveViaLocationInfo(city)        city pin -> Location Info -> Move
    ReadPlaceInfo(where)             city/port info without departing
    Invest(port)                     future
    FindEnemy(...)                   future

### 14.2 The caller never mentions the map

Today: `open_world_map()` is called, and THEN the work happens — the caller navigates, then
acts. Under the model the caller states a goal and the map is entered because something wants
doing there:

    RemoteCheck("Svear Village")        # says nothing about maps, globes or tabs

which is the same shape as `Hold(orders={...})` mentioning no tab and `Barter(good, village)`
mentioning no panel. `to_intent` turns the goal into `OPEN_WORLD_MAP(purpose=...)` when the
bot is not on the map, and returns None when it already is — the resolution that exists.

### 14.3 Is a complicated UI recipe the activity's business? YES.

The remote check is the hardest case: it opens Village Info, switches Base/Barter tabs,
scrolls a list, and recovers a quantity badge from its own tile. 167 lines with a scroll
loop. It is tempting to call that a "use case" and put it above the activity.

**The test is not "is it complicated?" but "is it about the SCREEN or about the MISSION?"**

Buying materials is at least as complicated — read the grid, buy, detect a sold-out shelf,
spend a blue gem, re-read, buy again, reconcile against the hold — and nobody doubts it
belongs to the market. The remote check is the same kind of thing: a sequence of screen
operations whose only product is a reading. It is a UI recipe, not a business task.

The counter-test is what makes that safe. It would NOT belong if the recipe made decisions
the mission owns — WHICH village to check, or whether the ratio is worth sailing for. Those
stay with the task runner. Opening a panel, switching a tab, scrolling a list and reading a
badge are interpretation of one screen.

Which is the project's guiding principle #1 applied to the hardest case:

> **Centralize the observation and the routing; localize the interpretation.**

The dispatcher gets the bot to the world map and perceives it. What the Village Info panel
MEANS is the world map's business, and nobody else's.
