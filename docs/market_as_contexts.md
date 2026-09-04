# Dialogs without sub-loops — the market as contexts

**Status: DRAFT, 2026-08-30.** The market is the chosen case study: it is the last activity
still driving whole flows internally, so converting it is how we find out whether the pattern
holds. Companion to `docs/capability_registry_design.md` and `docs/intent_graph.md`.

> *"in Android or any UI framework, you register handlers for UI controls, so a click on a
> button does not need to go through the intent dispatcher, but it is a listener registry that
> can call back when the action is performed. So you do not need a loop to wait for that. I am
> wondering how that paradigm can be used for subloops still in market."* — user, 2026-08-30

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

Goals stay as they are (`Hold`, `FreeHold`, `TrimHold`, `SellHold`) and become the second key,
exactly as `(handler, goals)` already works on the world map: a context that cannot serve the
current goal hands back instead of acting.

## What this dissolves, rather than guards

Both of today's market defects stop being reachable:

- **`Im glad` staged as a good.** The reader parsed a goods grid while a Result dialog was up
  — the dialog draws inside the goods zone, so a dialog row became a tile. As contexts,
  `result_dialog` classifies FIRST and "read the goods page" is not reachable from it. The
  price guard added on 2026-08-30 stops the tap; the context model stops the read.
- **`sold` reporting what was STAGED.** The list is built when tiles are tapped and never
  revisited. `result_dialog` is the natural and only place to record what actually sold —
  from `Total Amount`, which CLAUDE.md already names as the proof a transaction happened.

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
- **Is `negotiation` one context or several?** It has its own outcomes and may need a policy
  the goal carries, not the context.
- **What replaces the scroll loop?** Selling re-flows the grid, so "sell page → scroll →
  repeat" is a sequence. As contexts it becomes `sell_page` with a `scrolled` marker — which
  is state the activity holds, and state is what contexts are supposed to avoid.
