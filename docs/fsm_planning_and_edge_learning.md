# FSM Planning and Edge Learning — Architecture Direction

> **STATUS — SUPERSEDED IN PART (2026-05-02).**  This document introduced
> the planner concept and the edge / metadata foundations that
> [`planner_architecture.md`](planner_architecture.md) builds on.  The
> three-rule exploration policy (Rule 1 / 2 / 3), capability registry,
> and edge.update_history concepts are still valid — but they live INSIDE
> the planner's lookup-and-generate function, not as a separate selection
> module, and the unifying concept is now **plan**, not **edge**.
>
> Retained as foundational reading.  The newer doc refines verification
> (light + heavy with cue catalogs) and unifies recipe / edge / flow /
> resolution under a single plan abstraction.

*This document captures a deliberate architectural shift in how the bot navigates
state and handles the unfamiliar.  It does NOT describe the current code; it
describes where the bot is heading.  The current implementation is the starting
point and is referenced for contrast.*

---

## The reframe in one sentence

**Recovery is not a separate concern — it is FSM traversal with on-demand edge
learning.**

Or, said differently: the bot doesn't have a "main path" and a "recovery path."
It has a state graph, a goal, and a planner.  When the planner finds no path
from the current state to the goal, it invokes a learning layer that produces a
new edge and writes it back to the graph.  The next plan succeeds.  There is no
recovery code — there is only navigation, with edges added as they are
discovered.

---

## The problem with "recovery"

Today the codebase has two parallel sets of concerns:

- `brain/perceive.py` identifies the current state.
- `brain/recovery.py` handles "stuck" — it has its own BFS, its own escalation
  chain (Claude → human), its own concept of "assert_at_port", and its own way
  of advancing flows.

This shape was an artifact of the bot's history (see
`docs/journey_not_enough_crew.md` Prequel).  Recovery was added as a patch over
an FSM that wasn't yet complete.  It worked, but it kept the FSM and its escape
hatch as separate things.

The cost of that separation showed up most recently as a confidently-wrong
perception (a Notice dialog at the inn was perceived as `sea_cinematic` because
the dialog overlay hid the home button).  Recovery trusted the perception
verbatim, branched into "wait for natural arrival at sea", and timed out.  The
goal layer *knew* the fleet had not departed (phase = `FLEET_CHECK`) — but that
context never reached recovery, because recovery and goals are layered
parallel, not nested.

The deeper failure isn't "recovery wandered into sea handling."  It's that
"stuck" was treated as a distinct mode of operation, fenced off from the FSM
and the goal layer.  In a complete FSM, stuck is just a state with no
outgoing edge to the goal — and the response is *learn the edge*, not
*invoke a special escape*.

---

## The new model

```
        ┌──────────────────────────────────────────────────────────────┐
        │  perceive(frame)        →  State (with evidence + name)      │
        │  planner.next_edge(s,g) →  Edge | None                       │
        │      None  →  learn_edge(s, g)  →  new Edge stored, retry    │
        │      Edge  →  execute(edge)                                  │
        │  perceive(frame)        →  did we reach the expected state?  │
        │  edge.update_history(...)                                    │
        └──────────────────────────────────────────────────────────────┘
```

That is the entire main loop.  No `recover_to_port_overworld`, no
`assert_at_port`, no `advance_mandatory_flow`, no `_ensure_fleet_ready` as a
special case.  Every interaction is **perceive → plan → execute → update**, with
**learn-on-gap** as the response when the plan returns `None`.

Three core abstractions:

1. **State** — what the bot sees, named when known, evidenced when not.
2. **Edge** — a transition from one state to another, with provenance and
   history.
3. **Policy** — how the planner picks an edge when multiple are available
   and how it triggers learning when none are.

---

## State — rich evidence even when "unknown"

> `unknown` was the wrong word.  What the bot has at first encounter is a
> state whose **identity is not yet learned but whose evidence is rich**.

```python
@dataclass
class State:
    # Identity — initially provisional, promoted when learning succeeds
    id:        str        # 'not_enough_crew_dialog' once promoted; 'provisional_<sig>' before
    promoted:  bool       # has the reasoning layer named this state?

    # Evidence the reasoning layer uses to figure out what state this is
    # and what to look for in building outgoing edges:
    ocr_tokens:       list[str]
    visible_buttons:  list[(label, bbox)]
    chrome_signals:   dict        # back / home / ? / right_panel etc.
    npc_present:      bool
    npc_says:         Optional[str]
    qwen_detail:      str
    yellow_button:    bool
    confidence:       str         # 'high' | 'medium' | 'low' | 'unverified'
```

Two consequences:

- **Promotion is structural.**  The reasoning layer's job is to take a
  provisional state with rich evidence and promote it to a named state in the
  FSM.  Once promoted, the state has a stable identity and can accumulate
  edge-history.

- **Confidence is first-class.**  Today's `where_am_i()` returns a definitive
  state even on weak signals.  The new model lets perception say "I have
  evidence X, Y, Z; my best guess is sea_cinematic but I'm not sure" — and the
  planner can refuse to plan from a low-confidence state, instead invoking
  learning.

---

## Edge — provenance and history are first-class

```python
@dataclass
class Edge:
    id:          str
    from_state:  str
    to_state:    str
    actions:     list                # taps / waits / keypresses

    # Provenance & confidence
    provenance:  str                 # 'hand_authored' | 'claude_guided' | 'human_taught' | 'distilled'
    confidence:  str                 # 'unverified' | 'low' | 'medium' | 'high'

    # History — drives exploration policy
    travel_count:        int = 0
    success_count:       int = 0
    failure_count:       int = 0
    avg_duration_secs:   float = 0.0
    first_traveled_at:   datetime
    last_traveled_at:    datetime

    # Lineage
    supersedes:  list[str]            # edges this replaces after refinement
    notes:       str
```

Provenance and history are not optional decorations — the exploration policy
*requires* them.  Two edges with `travel_count = 1` are A/B candidates;
two edges with `travel_count = 50` are mature options where one has a clearly
lower `avg_duration_secs`.  Without history, every edge is treated as gospel
on its first traversal — which is exactly how the recent `learned_building__
sea_cinematic__...` recipe got committed despite being structurally wrong.

**Self-edges are first-class.**  An edge whose `to_state` equals its
`from_state` is structurally identical to any other edge — it has predicates,
provenance, history, and quality metrics.  Self-edges encode "stay in this
state because the progress signal says we're still progressing" — see the
Dynamic states section below for how dynamic states use them as the default
"keep going" path.

---

## Dynamic states — per-tick edge selection

Some states are passive — once entered, they stay until the bot acts (a building
interior, a port_overworld with the player standing still).  Others are
**dynamic** — content changes without bot action: the fleet is sailing, the
character is running toward a building, a loading screen is in transit, combat
turns are advancing.  Dynamic states need re-evaluation on every tick, not
just on entry.

The mechanism is simple: every tick, the state asks *"given the new perceive
result, which of my outgoing edges fires?"*  Self-edges are first-class
outgoing edges (their `to_state` happens to equal `from_state`).  No separate
"watchdog" or "progression observer" subsystem — **the edge predicates do all
the temporal work**.

### Tick loop

```
tick:
  result   = perceive()
  history.append(result)
  edge     = current_state.choose_edge(result, history)
  execute(edge.actions)              # may be no-op for self-edges
  current_state = edge.to_state      # equals current_state for self-edges
```

`choose_edge` runs the state's outgoing edge predicates in priority order.
First match wins.  If no edge predicate fires, learning kicks in.

### Worked example — `sea_active_sailing`

```
sea_active_sailing
  ┌─────────────────────────────────────────────────────────────────────────┐
  │  Priority 1 — known interruptors (handled inline; resume in same state) │
  │    ├─ daily_news_popup            → tap_close_button_only               │
  │    ├─ perk_event_popup            → tap_close_button_only               │
  │    ├─ attendance_popup            → tap_close_button_only               │
  │    ├─ daily_login_reward          → tap_collect_or_ok                   │
  │    ├─ active_story_event          → tap_skip_or_advance_until_clear     │
  │    └─ android_connection_dialog   → tap_ok_then_wait_reload             │
  │                                                                         │
  │  Priority 2 — known transition edges                                    │
  │    ├─ → loading                                                         │
  │    │     when voyage_days_remaining hit 0 + loading screen detected     │
  │    │                                                                    │
  │    ├─ → sea_stalled                                                     │
  │    │     when voyage_days_remaining unchanged for N ticks               │
  │    │                                                                    │
  │    └─ → sea_dead                                                        │
  │          when fleet_death_recovery_screen interruptor visible           │
  │                                                                         │
  │  Priority 3 — self-edge (stay in state)                                 │
  │    └─ keep_sailing                                                      │
  │          when voyage_days_remaining decreased since previous tick       │
  │                                                                         │
  │  Priority 4 — unknown (no edge fired)                                   │
  │    └─ → unknown_<signature>                                             │
  │          when none of the above predicates matched.                     │
  │          Triggers the learning layer (see Policy / Capability sections) │
  │          to identify the state and propose an outgoing edge.            │
  └─────────────────────────────────────────────────────────────────────────┘
```

### What goes where — known vs unknown

The principle: **only well-understood, stable interruptors and transitions get
explicit edges.  Everything else lands in the unknown bucket so the learning
layer can build the edges over time.**

- **Known interruptors get explicit edges in the state.**  daily_news,
  perk_event, attendance, login_reward, story_event, android_connection — all
  are well-understood overlays the bot has handlers for.  Each gets a
  Priority-1 edge in dynamic states where they can fire.

- **Known transitions get explicit edges.**  arrival → loading, stall →
  sea_stalled, death → sea_dead.  These are the structural state changes
  whose detection logic is stable.

- **The self-edge.**  Tested last among the "stay or transition" predicates —
  if neither a known interruptor nor a known transition fired, but the
  progress signal says we're advancing, stay in the state.

- **Unknown is the learning trigger, not a list to enumerate.**  An overlay
  the bot has never seen (a new event popup, an unknown game-side dialog) is
  expected to fall through Priority 1 because it doesn't match any known
  interruptor's keywords.  It also won't match known transition predicates.
  And it'll often hide the chrome enough that the self-edge predicate
  (`voyage_days_remaining decreased`) can't read its progress signal.  So it
  falls through to Priority 4, which is the canonical "I don't recognise
  this — invoke the learning layer" path.

  Once learned, the new interruptor or transition gets persisted as an edge in
  the right priority bucket for the next encounter — Priority 1 if it's a
  transient overlay that resumes the same state, Priority 2 if it's a
  legitimate transition.

### What this collapses from earlier discussions

- **No separate "progression observer" subsystem.**  Progression criteria are
  just self-edge predicates with access to recent history.
- **No "stuck detector" / "watchdog timeout".**  A stall is a transition edge.
  When its predicate fires, the FSM transitions to a stall state with its own
  outgoing edges.
- **No "recovery" branch.**  A state with no edge to the current goal is just
  a state needing learning.  A state with edges only to "wait" and "escalate"
  is the same shape — those are real edges, executed via normal FSM mechanics.
- **The `_overworld_signature` re-tap check in `navigate_to_building`** is a
  local precursor of this pattern — the self-edge predicate for the
  hypothetical `port_running_to_building` state ("signature unchanged → still
  walking, don't re-tap").  That logic moves out of the action function and
  into the state's edge definitions when the migration lands.  Same code,
  different home.

### Implementation implications

For dynamic states, perceive needs to extract **typed progression fields**
from each frame so edge predicates can compare them across history:

| State | Progression field | Source |
|---|---|---|
| `sea_active_sailing` | `voyage_days_remaining` (int) | OCR of "N Days of Sailing Left" top-left |
| `port_running_to_building` | `port_overworld_signature` (tuple) | canonical building list (already used as `_overworld_signature`) |
| `loading_to_port` | `loading_text` (str) | OCR of loading-screen destination |
| `combat_turn_active` | `turn_number` (int) | OCR of turn counter (when combat is implemented) |

These fields are computed per-frame; perceive stays mostly stateless.  History
is kept in a small ring buffer the FSM tick consults when running edge
predicates.  No new module beyond an extension of the perceive output schema.

---

## Policy — three rules with sharp boundaries

The planner picks an edge from the available set using three rules in order:

### Rule 1 — Zero edges

```
candidates = edges_from_to(s, g)
if not candidates:
    return learn_edge(s, g)
```

No known edge from current state to goal → invoke learning.  Output: a new
edge with `travel_count = 1` after first successful traversal.  This is the
"uncharted waters" case — the bot has reached a state with no map of where
to go next.

### Rule 2 — One edge, one traversal — forced exploration

```
if len(candidates) == 1 and candidates[0].travel_count == 1:
    alt = propose_alternative_edge(s, g, exclude=candidates)
    if alt: return alt
    # if no alternative exists yet, fall through to using the single edge
```

A single data point is insufficient to declare an edge a winner.  The policy
**must** try an alternative on the second encounter.  This is forced
exploration — not random.  The first 2 traversals from a state form an A/B
test by construction.

The "alternative" comes from the **capability registry** (see below).  Without
a structured way to enumerate alternatives, this rule degrades gracefully into
"no alternative available, take the only edge known".  But the more the
capability registry covers, the more useful Rule 2 becomes.

### Rule 3 — Multiple edges, mature traversals — epsilon-greedy

```
if all(e.travel_count >= 2 for e in candidates):
    if random() < EXPLORATION_RATE:    # default 0.2
        return weighted_random(candidates_excluding_best)
    else:
        return best_by_quality(candidates)
```

80% exploit (pick highest-quality), 20% explore (pick a different option).
Quality is `(success_rate, -avg_duration_secs)` — succeed first, fast second.
Over time the best edge dominates while alternatives keep accumulating
data; if the leader's quality drops (e.g. game patch changes UI), the
secondaries are ready to take over without a relearning episode.

### Why three rules

- Rule 1 handles cold start.
- Rule 2 prevents commitment to a one-trial result.
- Rule 3 handles the steady state with controlled exploration.

The transitions between rules happen automatically as `travel_count`
accumulates.  No mode switches; just history-driven dispatch.

---

## Where alternatives come from — the capability registry

Rule 2 needs structured alternatives.  Three sources:

| Source | When it applies |
|---|---|
| Capability registry | Goal is a capability (e.g. `hire_crew`); registry returns all providers ordered |
| Claude proposes | "I've tried path X; what's another path from this state to the goal?" |
| Human suggests | If Claude can't think of one, escalate |

The capability registry is the structural source of truth.  A flow like
`not_enough_crew` resolution should not name a building (`inn`); it should name
a capability (`hire_crew`).  Providers self-register:

```
inn.recruit_crew    : capability = hire_crew
harbor.recruit_crew : capability = hire_crew
```

The planner queries `providers_of(hire_crew)` and gets both — ordered by some
default heuristic (proximity, last-success-rate, etc.).  Rule 2 then selects
the second-ranked entry on a forced-exploration call.

Without the capability registry, Rule 2 falls back to asking Claude for
alternatives at runtime — which works but is API-cost-heavy.

---

## The `not_enough_crew` case as canonical illustration

This is the case that motivated the redesign.

**First encounter** (~ April 29, hand-authored under fire-fighting pressure):
no edges from `not_enough_crew_dialog → ready_to_depart`.  Rule 1 fires.
Learning produces a 6-step recipe routing through the inn.  Edge stored:
`provenance = hand_authored, travel_count = 1`.

**Second encounter** (current bot today): Rule 2 fires.  The single edge has
`travel_count = 1` — the policy demands an alternative attempt.  Capability
registry returns `[harbor.recruit_crew, inn.recruit_crew]` for `hire_crew`.
The 6-step recipe is the inn path; the alternative is the harbor path.  Bot
tries harbor.  If it succeeds, a second edge appears, both `travel_count = 1`.
A/B comparison is now in place.

**Third encounter onward**: Rule 3 takes over.  Once both paths have a few
traversals, the harbor path's `avg_duration_secs` (~30s — already at the
harbour) crushes the inn path's (~5min round trip).  80% the bot picks
harbor; 20% it occasionally re-validates other candidates.

**Eventual outcome**: harbor path dominates; inn path's confidence drifts
down; eventually `inn` is marked `superseded_by: harbor` (or just retired).
The convention-bias bug self-corrects without a human edit — exactly what the
"learned but improvable" framing in `docs/journey_not_enough_crew.md` was
asking for.

---

## Open design questions

Two pinned down with provisional answers; one left open.

### Q1 — What counts as the same state across runs?

**Provisional answer: signature-as-id, with optional reasoning re-validation.**

A canonical signature (state class, OCR title, presence/absence of yellow
button, chrome flags, etc.) is the state's permanent ID.  Reasoning that
promotes a signature into a human-readable name (`not_enough_crew_dialog`)
adds a label; the underlying signature stays the identity key.  Future
encounters compute the same signature → match the same state record.

This is cheap and works as long as signatures are stable.  It enables
**cold-start carryover** — edges learned in run N apply in run N+1 because
state IDs are deterministic from signatures.  Optional Claude re-validation
when the bot is uncertain handles signature drift (game patch changing UI).

### Q2 — How does the bot know an edge "succeeded"?

**Provisional answer: the planner re-perceives after edge execution and
checks whether the new state matches `edge.to_state`.**

If yes: `edge.success_count += 1`, update `avg_duration_secs`.

If no: `edge.failure_count += 1`.  If failure_count exceeds a threshold,
demote `confidence` and trigger Rule 1 (no longer a viable edge to this goal
from this state).

### Q3 — What about edges that change other state we don't track?

**Open.**  Some actions have side effects (gold spent, supply consumed,
karma changed) that aren't captured by the state graph but matter for
selection.  The harbor recruit_crew costs ~218k gold; the inn route also
costs ~218k.  But what if the inn requires a contract gift (additional cost)?
The planner today has no notion of cost beyond duration.

For now: track `avg_duration_secs` only.  Cost-aware planning is a later
addition once we have any planner at all.

---

## Migration shape — three additive milestones

This does not have to land all at once.

### Milestone A — operational metadata

Add the `provenance`, `confidence`, `travel_count`, `success_count`,
`failure_count`, `last_traveled_at`, `avg_duration_secs` fields to existing
flow / edge records in `flows.json`.  No behaviour change yet; just stop
losing the data.  Pure schema addition.  Begins answering "is this learned
recipe trustworthy?".

Estimated scope: 1 day.

### Milestone B — exploration policy on top of existing edges

Implement Rule 2 / Rule 3 logic when there are multiple edges serving the
same `(from_state → to_state)` pair.  Until the capability registry exists
(Milestone C), this only applies where `flows.json` already lists alternatives,
which is rare today.  Useful as a vehicle to validate the policy code on
existing data.

Estimated scope: 1-2 days.

### Milestone C — capability registry + named alternatives

Add `capability` fields to `building_types/*.json` sub-menus.  Rewrite
blocker resolutions (starting with `not_enough_crew`) as
`needs_capability: hire_crew` directives.  The planner queries the
capability registry to enumerate alternatives — Rule 2 always has a
structured fallback.  This is when the inn-vs-harbor case becomes the
first natural test of the policy.

Estimated scope: 2-3 days.

### Milestone D — planner replaces recovery

Rename `brain/recovery.py` → `brain/planner.py`.  Generalize BFS-to-
port_overworld into BFS-to-arbitrary-target.  Update consumers
(`sail_to`, `task_runner`, etc.) to pass `goal_state` rather than relying
on hardcoded targets.

After this, "recovery" is vocabulary the codebase no longer uses.

Estimated scope: 2-3 days.

### Milestone E — unified learn_edge interface

`claude_guided_loop`, `escalate`, and the various save_* helpers collapse
into a single `learn_edge(from_state, target_state)` API.  Internally tries
Claude first, then human; output is a uniform `Edge` record with
`provenance` set appropriately.

Estimated scope: 1-2 days.

### Total

Roughly 1-2 weeks of focused work for the full migration.  But it can land
incrementally — each milestone is independently useful and the cumulative
effect is the architecture this document describes.

---

## What this does NOT change

The data flywheel concept from CLAUDE.md (Milestone 2.5) for vision:
unchanged.  Local models distilling from Claude labels remains its own
loop.

The KB structure for game-content knowledge (`memory/knowledge/control/`,
`memory/knowledge/strategy/`): unchanged.  These are inputs to the new
model — capabilities, indicators, conventions all feed into state and edge
definitions.

The interruptor pass in `perceive.py`: kept, but its role becomes more
focused.  KNOWN interruptors (the entries in `interruptors.json` —
daily_news, perk_event, attendance, login_reward, story_event,
android_connection, etc.) are still detected and dismissed as a Pass-1
concern before regular state classification — that's the same shape as
today.  In the new model, a state's Priority-1 outgoing edges *reference*
those known interruptors — when one fires, the state's edge selection
short-circuits to "dismiss the interruptor and resume in the same state".
UNKNOWN overlays are deliberately NOT enumerated as interruptors; they
fall through Pass 1 (no detection_keywords match), then through the
state's Priority-2/3 edges (no transition or self-edge predicate fires),
and land in Priority 4 — the unknown bucket — which is the canonical
trigger for the learning layer.  See "Dynamic states — per-tick edge
selection" above.

---

## Connection to the journey doc

`docs/journey_not_enough_crew.md` ends with five proposed additions for
"learned but improvable" architecture: provenance/quality metadata, learning
ledger, quality-aware selection, failure-driven refinement, domain
unification.  This document is what those five additions look like when
they are not separate features but a single coherent architecture.

The journey-doc's "uncharted waters" framing maps directly: charted = states
and edges with high `travel_count` and high `confidence`; uncharted = states
the bot is encountering for the first time, where Rule 1 fires.  The
cartographer's logbook is the edge history — every edge has its log of
`travel_count`, `success_rate`, `avg_duration_secs`, `last_traveled_at`,
`first_traveled_at`, `supersedes`.

---

## Decision deferred for now

The user has explicitly chosen to NOT apply two band-aid fixes that would
mask the failure modes this architecture is designed to handle:

- Returning `state='unknown'` from `where_am_i()` instead of falling through
  to `sea_cinematic` (would make Notice-dialog mis-perception self-recover via
  `reclassify_with_claude`).
- Passing goal-phase context into recovery to reject impossible state
  transitions (would catch the `building → sea_cinematic` teleport).

The reasoning: with these band-aids in place, the architectural argument for
the migration weakens — "well, recovery isn't perfect but mostly works".  By
leaving the failure modes exposed, the next live runs surface them as
recurring evidence that the structural fix is the right intervention.  Each
incident becomes data for the design.

The bad learned flow `learned_building__sea_cinematic__20260501T053142Z`
is being purged separately so the bot relearns rather than replays a known-
broken recipe — but no other patch is being applied between now and the
migration work.
