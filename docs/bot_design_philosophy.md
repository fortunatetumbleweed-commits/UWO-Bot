# Bot Design Philosophy — What to Build In vs What to Learn

> **STATUS — STILL CURRENT (2026-05-02).**  The principles here
> (what to hardcode vs what to learn) are unchanged.
> [`planner_architecture.md`](planner_architecture.md) is the concrete
> realisation: hand-authored plans are explicit starting points
> (provenance='hand_authored'), and the bot learns by amending those
> plans through replan and accumulating cue catalogs through use.

## The Core Question

What should be hardcoded into the bot, and what should it discover or learn?

The answer organises into three layers: **mechanisms** (how to act), **knowledge**
(facts about the world), and **policy** (what to do).

---

## Layer 1 — Built In: Mechanisms

These are *how* actions get executed. They don't change regardless of strategy.
They should be reliable, deterministic, and heavily tested — the rest of the bot
depends on them.

- **FSM + Perception** — hierarchical state machine with `port_overworld` as the floor;
  three-pass perceiver (interruptors → active flow → navigation state) ← in progress
- **Screen navigation** — port map, world map, building entry/exit ✅
- **Location oracle** (`perceive()`) — always know the full state: navigation state,
  active flow if any, active interruptors ← replacing `where_am_i()`
- **Recovery** — `recover_to_port_overworld()`: BFS on state graph back to floor;
  handles sea→port via world map; never breaks atomic flows ← in progress
- **Market transactions** — buy, sell, read prices ✅
- **Sailing** — depart, navigate world map, arrive ✅
- **Resupply** — dock at intermediate port, restock supply

---

## Layer 2 — Knowledge Base: Rules + Facts

Things that are true about the game world and don't need to be re-discovered each run.
Built once, reused forever. The bot reads this before deciding what to do.

- **FSM registry** — all known navigation states, atomic flows, and interruptors with
  detection rules, transition actions, and recovery paths; grows as the bot encounters
  new screens (`memory/knowledge/fsm/`) ← new
- **Port database** — what buildings each city has, what goods it trades
- **Cultural preferences** — Islamic cities don't buy pigs/alcohol (price = 0, not just low);
  other cultures have their own restrictions and preferences
- **Sea region requirements** — which fleet stats (seaworthiness, momentum, ice-breaking)
  are needed to enter which waters
- **Route graph** — which ports connect to which, typical voyage days, resupply stops
- **Market history** — price snapshots over time, which goods are profitable on which routes

New facts (a newly discovered cultural rule, a sea region requirement, an unknown dialog
flow encountered for the first time) are added to the KB and the bot automatically
respects them everywhere — nothing needs to be re-hardcoded.

---

## Layer 3 — Policy: What to Do

The *decisions* — given the current state, what is the best action.

- **Which route to trade** — based on price history, current market events, time of day
- **Which goods to buy** — beyond the recommended list; recognising when a
  non-recommended good is actually better
- **When to explore vs trade** — when prices are flat, exploration earns Adventure EXP
  rather than sitting idle
- **When to invest in a port** — if the bot trades through it regularly, mayor control
  gives price control advantage
- **When to upgrade ships** — ducat threshold + route unlock opportunity
- **Combat decisions** — attack, flee, or use Repel Support

Policy starts as **hardcoded heuristics** (e.g. "buy below 90%, sell above 110%") and
is gradually replaced by KB-driven scoring and eventually adaptive behaviour.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  BUILT IN (mechanisms)                                      │
│                                                             │
│  FSM + perceive → act → recover                             │
│    perceive():   1. clear interruptors                      │
│                  2. detect active flow                      │
│                  3. detect navigation state                 │
│    recover():    BFS on state graph → port_overworld        │
│                  sea path: open world map → sail home       │
│                                                             │
│  Navigate, tap, OCR, buy, sell, sail                        │
└────────────────────┬────────────────────────────────────────┘
                     │ consults
┌────────────────────▼────────────────────────────────────────┐
│  KNOWLEDGE BASE (rules + facts)                             │
│                                                             │
│  FSM registry: states, flows, interruptors, recovery paths  │
│  Port data, cultural rules, route graph,                    │
│  market price history, sea region requirements              │
│                                                             │
│  Grows autonomously: unknown state/flow → Claude Vision     │
│  → new entry added → never asked again                      │
└────────────────────┬────────────────────────────────────────┘
                     │ informs
┌────────────────────▼────────────────────────────────────────┐
│  POLICY (what to do)                                        │
│  Phase 1: hardcoded heuristics                              │
│  Phase 2: KB-driven scoring (best route/goods)              │
│  Phase 3: adaptive — learns from outcomes                   │
└─────────────────────────────────────────────────────────────┘
```

---

## What This Means for the Roadmap

**Right now** the bot has solid mechanisms (Milestones 1 & 2 complete) and is running
autonomous trade rounds on the London ↔ Port Royal route via `self_grow`.

**Milestone 3** (FSM & robust recovery) is the current focus. It makes the mechanism
layer reliable enough to run unattended. The bot currently gets confused by the main menu,
atomic flows (negotiation, purchase result), and unexpected sea states. The FSM registry
gives it a complete map of what states exist, how to detect them, and how to get back to
`port_overworld` from any of them. Without this, every new screen the bot encounters is
a potential infinite loop.

**Milestone 4** (world map intelligence) builds the knowledge layer — City Info panels,
port database, cultural preferences, route graph with resupply points.

**Milestone 5** (route optimisation) implements policy layer phase 1 — use the KB to
score routes and pick the best one automatically.

**Milestones 6–8** progressively deepen all three layers — more buildings understood,
more cultural knowledge, better scoring, eventually adaptive self-improvement and the
ability to unlock progressively harder sea regions (Pacific, Arctic) as the company's
fleet stats improve through earnings.
