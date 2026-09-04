# What can be done HERE — an activity capability registry

**Status: PROPOSED, 2026-08-30.** Supersedes the `SERVES_FOR` sketch in
`docs/per_goal_serving.md`; complements `docs/intent_graph.md`. Written from the user's
framing after a day in which activities were repeatedly handed work they could not do.

> *"activities like village were handed tasks that can not be done there, like check
> capacity, or trim surplus… we need to have a registration of the tasks that an activity can
> perform… The dispatcher would not dispatch to those activities of these tasks. And an
> undispatched task should be logged and a potential issue."* — user, 2026-08-30

## Two facts, currently tangled

**CAPABILITY — can this work be done here?** A village can barter, gift and recruit. A market
can buy and sell. The main menu (the ☰) exists only on the overworlds. This is what the
registry holds.

**NAVIGATION — how do I reach a place where it can?** The four intents as edges between
screens. That is `docs/intent_graph.md`, and it is a different question.

They meet at one point and nowhere else: when the work cannot be done here, the registry says
so, and the graph finds where it can. Today neither exists, so both questions are answered by
whichever hand-maintained list happens to be nearest.

## The registry

Each activity declares the WORK it can perform, not merely the screens it appears on. Today
it declares only `SERVES` (screens) and a flat `GOALS` tuple, which cannot express that
`AshoreActivity` serves `ArriveAshore` at a village but `ReadHold` only where the ☰ is.

```
village          barter · gift · recruit · read the trade list
market           buy · sell · trim
harbour          depart · recruit crew
world map        choose a destination · read the trade event schedule
sea              arrive ashore · read the hold (☰)
port overworld   read the hold (☰) · enter a building · open the map
```

**An undispatched work order is a defect, and must be logged as one.** Not silently skipped,
not failed — reported, with the work order and the activity that refused it. Every wedge
yesterday was this event happening invisibly, discovered only when a run stopped.

## The overworld invariant

> *"the overworlds are the only places you can not back, they have the hamburger menu, and
> title as back"* — user, 2026-08-30

One fact with three faces:

```
overworld  ⟺  has the ☰  ⟺  Back is not available (it raises "Exit Game?")
elsewhere  ⟺  no ☰       ⟺  the chromed title bar IS the back button
```

This is worth stating because the codebase holds pieces of it in disagreeing places.
`_NEVER_BACK_FROM = ("port_overworld", "main_menu")` — **`sea` is missing**, though it is an
overworld and carries the ☰. Nothing has pressed Back at sea yet, so nobody has noticed.

### And `main_menu` does not belong in that list at all

It is not a world. It is an OVERLAY over one — the overworld stays visible beneath it — and
Back dismisses it. The codebase already contradicts itself here:

```
brain/intents.py:40           _NEVER_BACK_FROM = ("port_overworld", "main_menu")
actions/fleet_status.py:114   ui.back(why="close the main menu")
```

and the second one works: `[ui] back — close the main menu`, twice in run 7, both successful.

**It is a CONTEXT, not an activity** (user, 2026-08-30) — exactly what `City Info` and
`Village Info` are to `WorldMapActivity`. Opening and closing it changes the screen without
changing the world, so it is not a transition (Guiding Principle #1).

What makes it unusual is worth recording: **it is self-contained and identical in both
overworlds**, so it is ONE context with ONE reader, hosted by two activities — `SeaActivity`
and `AshoreActivity`. Not duplicated, and not promoted to a world of its own.

Two consequences:

- `ReadHold` is not a capability of the main menu. It is a capability of **the overworlds**,
  reached THROUGH a context they both host. That is the real reason it works at sea and at a
  port and not in a village: not that those screens carry the fleet panel, but that they can
  open it.
- `actions/fleet_status.py` currently opens the menu, reads, and backs out on its own — a
  primitive navigating into and out of a screen the dispatcher does not know it is in. That
  is the sub-loop shape (Principle #5). As a context, entering it is the activity's own
  business and the read is a local interpretation, with no private navigation at all.

## The task is gather, barter, sell — the rest is support

> *"check load is not a work order… the task should only consist of gathering, bartering,
> selling, all the others are supportive… so there will be no mandatory trim at every step,
> rather it is optional and only be dispatched when market is available."* — user

This is the sharpest part, and it dissolves a whole class of failure rather than guarding it.

Today the mission graph carries `trim_before_gather`, `sell_surplus`, `supply_verify` and a
startup hold read as LEGS — first-class steps that must be selected, then run or skipped.
That is why a run starting at Svear picked the trim, could not do it, and had to be taught
that a village has no market. And why another read the hold at a village and died on tick 2.

As SUPPORT they never arise there at all. Support is opportunistic: taken when the place
affords it and the mission benefits, never scheduled, never blocking. The trim happens
because the fleet is standing in a market with surplus aboard — not because step 1 of 9 says
so.

What that removes, all of which was hand-patched yesterday:
- `_ASHORE_ONLY` / `_NO_MARKET` — a leg that only exists where it can run needs no location test
- the optional-leg skip path in `_start_next_leg`
- the startup hold read (already removed; capacity is remembered instead)
- `_departed_for_the_village` settling seven legs at once, because six of them were support

## Where it lives

The user's lean: **the dispatcher layer** — it already holds the activity registry, and it is
the one place that knows both the current activity and the work order. That also keeps the
task runner passive (Guiding Principle #7): the runner still only answers "what next", and
the dispatcher decides whether it can be done here or whether routing is needed first.

The alternative — putting it in the task runner — would require the runner to know about
screens, which is exactly the coupling #7 exists to prevent.

## Open questions

- Does a capability belong to the ACTIVITY or to the SCREEN? A village activity can barter,
  but only on a screen that offers the Barter menu item. Frame 15 of run 11 shows that item
  wearing a red `Cannot Exchange` ribbon — the capability is present and *currently* refused,
  which is a third state beyond can/cannot.
- How does support get its chance? Something must notice "we are in a market and carrying
  surplus". A hook after every activity? A standing list of support actions the dispatcher
  offers the mission when the place affords them?
- Is `supply_verify` support or a genuine precondition of the village leg? It gates a voyage,
  which is not opportunistic.
