# The plan is a checklist of observable conditions

**Status:** DESIGN, 2026-09-01. The capability half is built; the checklist half is not.
Companion to `bot_architecture_layers.md`, which names the layers — this says what has to be
true of the middle one for it to work.

---

## The claim

> The decomposition of a task is a **function of (business need, game knowledge, current
> state)**, evaluated every time the manager is consulted — not a plan computed once and
> walked. An item is done when **the world says so**, not when an action reported success.

Both halves are CLAUDE.md Guiding Principle #2 applied to the plan itself:

> *Never store a conclusion — store the observation, recompute the conclusion. The test:
> could a fresh screenshot prove this false?*

A fresh screenshot can prove "the sail leg is done" false. On 2026-09-01 it did, for twenty
minutes.

---

## The three roles

The split is the user's (2026-09-01), and matches `bot_architecture_layers.md`:

| role | knows | says |
|---|---|---|
| **the company manager** | the business only | "we still need materials" |
| **the planner** | game knowledge + current state | "sail to Faro; then buy pigs" |
| **the dispatcher** | worlds and their controls | "tap the globe" |

The manager never learns what a `sub_menu:purchase` is. The planner never taps anything. This
is already the shape of the code: `brain/mission_progress` and `barter_command` hold the
business, `brain/mission.py` + `barter_mission_live.catalogue_coords()` hold the game
knowledge — it is what turns "gather materials" into `gather:Barcelona`, `gather:Tripoli` —
and the dispatcher owns the rest.

**So the layer is not missing. What is missing is *when* the middle one runs.**

---

## What is wrong today

### 1. The plan is computed once

`build_barter_graph` runs at plan time, reads `current_position()` once, bakes the leg order,
and `MissionRunner` walks the list. The catalogue ("Faro has pigs") is an **observation** and
is rightly cached. The leg order, and which part of a leg is behind us, are **conclusions**
drawn from a position that has since changed.

### 2. Done is a flag, not a question

```python
if runner.status == DONE:
    leg.done = True          # set from the RUNNER'S self-report
```

`done` records that an *action* reported success. Nothing asks the world.

Live 2026-09-01: the fleet tapped **Supply Departure** at Tripoli with no destination set.
`sail_runner` logged *"the harbour confirmed the departure — watching for the sea rather than
asking"*, treated the sail part of `gather:Barcelona` as behind it, and advanced to the buy
part. At sea, consulted for work, the mission offered **buy** — and kept offering it. The
condition was too loose: *departed* is not *arrived*.

### 3. The consequence

`ENTER_BUILDING` went out at sea on every tick. `tap_building_entry` refused every time —
correctly — and a refusal concludes nothing, so the next tick asked the identical question.
In between it read the minimap's tab strip as the building list's and cycled its four icons
for minutes.

---

## What the checklist changes

**The structure is already right.** `SubTask` carries `deps`, not an index:

```python
class SubTask:
    deps: tuple = ()        # ids that must be done first
    done: bool = False
    optional: bool = False
```

> *"`deps` are ids that must be `done` first; independent sub-tasks (the gathers) have no
> deps and are ordered dynamically by cost."*

So the gathers are genuinely unordered and the tail is a dependency chain. Items may be
checked off in different orders; only real dependencies constrain. That is the checklist.

**Two things change:**

**Split a leg into its checkable parts.** "Gather Iron at Barcelona" is two things (user,
2026-09-01):

```
at(Barcelona)              done ⟺ the perceived port IS Barcelona
buy(Iron, N)               deps=(at:Barcelona,)   done ⟺ the hold holds N
```

**Make `done` a predicate.** Each item carries a question answered by looking, and *departed*
is then not expressible as progress at all — there is no flag for it to set.

Together these make the 2026-09-01 failure unrepresentable rather than guarded: the fleet
never satisfied `at(Barcelona)`, so `buy(Iron)` was never eligible, so the mission would have
been asked for work whose first unmet item was *get to Barcelona*.

### The pattern already exists — once, by name

```python
# Standing in the village settles it too, as a backstop: being here is what
# "got here" means, and it costs nothing to notice.
```

`mission_runner._arrived_at_the_village` already does exactly this, for one arrival.
Generalising that sentence is the change.

This is a recurring shape in this codebase: the right rule is discovered, applied to the one
case that hurt, and not generalised. Three instances found on 2026-09-01 alone — `to_intent`
guarding `world_map` and no other world, `ensure_sell_tab` sitting correct beside a broken
twin, and this.

---

## Why it also fixes routing

A capability check (below) can say *"a market cannot be entered from the sea"* and decline.
But declining only helps if the manager, asked again, has something else to offer — and a
manager reading a **baked plan** does not. Re-derive from the live state and the answer falls
out: needing Barcelona, at sea, the first unmet item is `at(Barcelona)`, whose work order is
*sail there*.

**Declining is not routing.** The checklist is what makes the decline useful.

---

## What is built (2026-09-01)

Each activity declares what can be STARTED in the world it serves, and `affordances()`
derives the answer — the same way `default_activities()` derives the registry, and for the
same reason:

```python
class SeaActivity:
    SERVES    = ("sea", "sea_cinematic")
    CAN_START = ("OPEN_WORLD_MAP",)          # the minimap. No buildings at sea.

class AshoreActivity:
    SERVES    = ("port_overworld", "village")
    CAN_START = {"port_overworld": ("OPEN_WORLD_MAP", "ENTER_BUILDING"),
                 "village":        ("EXIT_BUILDING",)}
```

The dispatcher checks before dispatching; on a mismatch the task is told through the `result`
channel it already reads and asked **once** more, and a second unservable order is logged as
a **defect** rather than skipped.

Two facts the code forced:

- **Per world, not per class.** `AshoreActivity` serves `port_overworld` *and* `village`,
  which differ precisely where the earlier wedge was.
- **Silence is not refusal.** An undeclared world returns `None`, never an empty set.
  `PositionKnownActivity` declares `()` though it serves all fifteen workable states.

### The two questions, and why they are still two

`affordances(where)` — can this world START this transition? *(the dispatcher)*
`port_is_underfoot(where)` — is a market at most a door away? *(the mission)*

These were two hand-written lists — `CAN_START` and `mission_runner._NO_MARKET` — free to
drift. `port_is_underfoot` now derives its answer from the same `SERVES` declarations, and
`tests/test_a_world_declares_what_can_start_in_it` pins them to agree.

They remain two questions on purpose. **`CAN_START` says what a world can start, never where
that lands.** From a building you step out and into another, so a market is two transitions
away; from a village you also step out — and land at **sea**. Same declared capability,
opposite answer. Unifying them needs the destination of each transition, which is
`docs/intent_graph.md`, still open.

---

## Open

- **`SubTask.done` as a predicate**, and splitting a gather into `at(port)` + `buy(good)`.
  Not started — a required leg that cannot run fails the mission, so this needs care.
- **The intent graph.** Until transitions declare where they land, reachability cannot be
  computed and the two capability questions stay separate.
- **Re-deriving the plan on every consultation** rather than walking a baked graph. The
  catalogue stays cached; the decomposition does not.
