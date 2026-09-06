# The market refactor — action plan

**Companion to `docs/market_as_contexts.md`**, which holds the rationale, the four failure
cases (FC-1…FC-4) and the governing architecture. This file is the plan: what changes, what
does not, and how each step is checked. Branch: `market_refactor`.

## The shape we are moving to

```
dispatcher   perceive  ->  dispatch
activity     handle the perceived request -> modify context -> take action
             -> hand back
```

The activity becomes **a table of small callbacks the dispatcher owns**. Each one:

1. is chosen by the CONTEXT the dispatcher just perceived (plus the goal),
2. reads only what that observation already gives it,
3. mutates the market **context object** — never the world's state, never a belief about a
   screen it has not seen,
4. takes at most the actions that one observation justifies,
5. returns `WORKING` with a **recorded intent** (`{"did": ...}`) and hands back.

`{"did": ...}` is not a log line. It is the record of what this tick attempted, so the NEXT
tick can tell "my tap landed" from "nothing happened" without holding control to find out.
The village already does exactly this (`"did": "committed a round"`, `"did": "selected Argan
Oil"`), and `brain/barter_runner._absorb` already reads it.

**Nothing is rewritten.** Every reader, tapper and parser stays; they are re-arranged so that
the waiting between them belongs to the dispatcher.

## What changes, and what does not

### Loops that MUST dissolve — control is held across a capture

| where | loop | why it must go |
|---|---|---|
| `commit_actions.py:419` | `for iteration in range(max_taps)` | **FC-3.** Captures each iteration; tapped `Receive` then `OK` on a discard notice with no dispatcher tick between |
| `buy_materials.py:1131` | `for attempt in range(max_rounds)` | the buy round loop — `_read_grid()` per iteration; FC-2 and the stale-ledger runaway live here |
| `sell_goods.py:277` | `for r in range(max_rounds)` | the sell round loop — perceives per iteration; FC-4 lives here |
| `buy_materials.py:264` | `for _ in range(4)` + `sleep(1.0)` | `_react_after_purchase`, a private dialog waiter |
| `buy_materials.py:976` | `for _ in range(3)` | a read retry while seeding the ledger |

### Loops that STAY — batched taps inside one observation

| where | loop | why it is fine |
|---|---|---|
| `buy_materials.py:171` | `for material in targets:` | taps tiles located in the SAME `els` read; no capture inside |
| `sell_goods.py:327` | `for g in sellable:` | stages goods located in the same page read |

These are the batching rule in `market_as_contexts.md`: *a tick is one observation → one
decision → the actions that decision entails.* Deleting them would buy nothing and cost
perceives.

### Functions that move unchanged

Readers (`read_market_page_omni`, `_read_owned_via_sell`, `fill_missing_quantities`), tappers
(`purchase_goods`, `ensure_sell_tab`, `refresh_market`'s tap steps), the ledger, and every
guard listed under "What must not regress". They are called BY handlers instead of by loops.

## The context object

`buy_to_goal`'s locals are already the market's state; today they live for one call. They
become fields on a `MarketContext` the activity holds, keyed to the goal (the village's
`_goal_key` pattern — reset when the goal changes, so nothing outlives its owner):

```
ledger              what this visit bought/sold          (exists: brain/market_ledger)
tab                 purchase | sell | unknown
tracked_pos         the cargo tile being followed
attempts            per-control counter — the retry bound, replacing every `for attempt`
last_intent         what the previous tick did ("staged Raisin", "tapped Purchase")
last_signature      the observation the intent was taken against
session_bought      cargo-total delta since the visit began
scrolled_pages      sell-page position
```

`last_intent` + `last_signature` are what make a dropped tap detectable without a loop: the
next tick compares the new observation against the signature the intent was taken on. Same
judgement as `barter_panel._try`, with the waiting moved out.

## The handler table

Contexts from `market_as_contexts.md`, corrected against frames. One action each:

| context | handler does | then |
|---|---|---|
| `market_landing` | open the tab the goal needs | return |
| `purchase_page` | stage the goods this observation justifies, or commit | return |
| `sell_page` | stage, or commit, or scroll | return |
| `quantity_dialog` | type the amount | return |
| `confirm_dialog` | OK | return |
| `result_dialog` | **READ it, write the ledger**, OK | return |
| `negotiation` | accept/decline per goal policy | return |
| `restock_prompt` | blue gem yes, red gem never | return |
| `overflow_prompt` | hand to the dumping policy | return |
| `discard_notice` | answer per policy | return |
| `cargo_full_notice` | stop buying; the hold, not the shelf, is the problem | return |
| `MISS` | **hand back** — the dispatcher owns it | return |

Only `result_dialog` may write purchases/sales to the ledger. That makes FC-2 unreachable.

## Steps, each checkable against recorded runs

We have the sessions to check against — `data/sessions/trace_barter_cmd_2026-09-05T*` carry
242–429 frames each, with the OmniParser parse already attached to most of them.

1. **`brain/market_context.py`, classify-only.** No behaviour change. Run it over every
   recorded market frame and check each lands somewhere sensible.
   *Check:* every frame classifies; FC-3's frame 214 lands on `overflow_prompt`, not
   `village_top_menu`; no market frame lands on `MISS` that a human would name.
2. **The `MarketContext` object**, populated but not yet driving. Move `buy_to_goal`'s locals
   onto it; the loop still runs.
   *Check:* the existing suite passes unchanged.
3. **Convert the READ path** (`_read_owned_via_sell`, page reads) to go through
   `perceive_repository` so a parse is made once per observation.
   *Check:* reads per buy cycle drop from 3–4 to 1; measured on a replayed session.
4. **Convert the commit chain** — `commit_via_positive_taps` becomes
   `confirm_dialog` / `result_dialog` / `negotiation` handlers.
   *Check:* **FC-1 and FC-3 replay to a different outcome.**
5. **Convert the sell flow**, `sell_goods` kept callable until handlers cover it.
   *Check:* **FC-4 replays to a different outcome.**
6. **Convert the buy flow** — `buy_to_goal`'s rounds become ticks.
   *Check:* **FC-2 replays to a different outcome**; a live gather still completes.
7. **Delete the private waiters** once nothing reaches them.

## Acceptance

Mechanical, from the architecture: **for every function in the market path, can control be
inside it while the screen shows something it did not expect?** Done means no, everywhere.

Not "fewer loops", and not "careful loops" — `commit_via_positive_taps` was careful.

Plus, per step: the four failure cases replay to different outcomes, the regression guards in
`market_as_contexts.md` still hold, and a live mission completes end to end.
