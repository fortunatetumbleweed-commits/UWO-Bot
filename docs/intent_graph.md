# A graph for intents — navigation as a distinct role

**Status: PROPOSED, not decided.** 2026-08-30. Extends `docs/per_goal_serving.md`, which
recorded the symptom; this records the shape the user named for the cure.

> *"we may need something that right now is in the dispatcher, but may need a more distinct
> role, that manages the activity navigation… Worldmap now is the only place that can set a
> sailing destination… So it is really who can send an intent to open world map. Market surely
> can not. So we may need to define a graph for intents."* — user, 2026-08-30

## The claim

There are exactly four intents, and each is an EDGE between screens:

```
OPEN_WORLD_MAP    port_overworld ─► world_map        the globe
                  sea            ─► world_map        the minimap
                  port_map       ─► world_map        its 'World Map' button (unverified)

CLOSE_WORLD_MAP   world_map      ─► whatever it covered

ENTER_BUILDING    port_overworld ─► building:X       the building list

EXIT_BUILDING     sub_menu:X     ─► building:X       back
                  building:X     ─► port_overworld   back
                  village        ─► sea              back
```

An edge exists where the SCREEN CARRIES THE CONTROL. That is the fact
`docs/per_goal_serving.md` found written three times in disagreeing lists
(`_TAIL_CAN_SAIL_FROM`, `tap_world_map_control`'s branches, `_is_inside`).

Read as a graph, the day's failures are all the same one: **no edge, and no search for a
path.** `to_intent` answered with a single edge and, when none existed from here, either
emitted one anyway or gave up.

| live failure | what the graph says |
|---|---|
| `OPEN_WORLD_MAP` from `sub_menu:sell` | no such edge; path is `sub_menu:sell ─► building:market ─► port_overworld ─► world_map` |
| `OPEN_WORLD_MAP` at `village` | no such edge; path is `village ─► sea ─► world_map` |
| `ReadHold` at `village` | the goal needs a screen with the ☰; nearest is `sea`, one edge away |

Each was patched by hand at the branch where it surfaced. A path search answers all three
without any of them being special.

## Why "who may emit it" is the right question

Setting a sailing destination happens in exactly ONE place — the world map. A market cannot
open it; nor can a village. So the reachability question is not decoration, it is the whole
of navigation: every voyage in the mission depends on getting from wherever the fleet is to
the one screen that can set a course.

Note this is about the SOURCE STATE, not about the activity. Activities never dispatch —
only the dispatcher does (Guiding Principle #1, #7). "Market cannot send OPEN_WORLD_MAP"
means *there is no edge out of a market to the map*, which is a fact about the screen.

## What it would subsume

- `_is_inside` — "anywhere with a Back and no ☰" becomes: has a back edge, has no ☰.
- `_TAIL_CAN_SAIL_FROM` — states with an outgoing OPEN_WORLD_MAP edge (or already there).
- `_NEVER_BACK_FROM` — a MISSING back edge, not a special case. Back at `port_overworld`
  raises "Exit Game?", so there is simply no edge; nothing needs to remember not to press it.
- `tap_world_map_control`'s per-state branches — each edge carries its own action, so the
  primitive stops re-deciding what the router already decided. This is what let the two
  disagree and wedge a run three times.
- `SERVES_FOR` (from the per-goal note) — a goal names the screens that serve it; that set is
  the TARGET of the search, not another table to maintain.

## The rule that keeps it safe

**The graph gives the next intent, never a plan to execute.** Each tick: perceive, find the
current state, search for the nearest state that serves the goal, dispatch the first edge —
and then look again. Where the tap actually landed is observed, not assumed.

That preserves Principles #0 and #5. A path executed blind would be the sub-loop this
architecture exists to remove: it would keep walking a route the world had already left. The
existing "do not re-dispatch an intent still in flight" guard stays exactly as it is.

## Open questions

- Where does the graph live? It is a perception fact (what a screen affords) consumed by
  routing. On the state, or a table beside `NAV_STATES`?
- Edges are conditional: the village list icon differs by tab, `port_overworld` refuses the
  globe when the left menu is a village's. Are those edge PREDICATES, or separate nodes?
- `CLOSE_WORLD_MAP`'s destination is "whatever it covered" — an edge whose target is unknown
  until observed. Fine under the tick rule above, but it means the graph is not fully static.
- Manual sea navigation (steering without a destination) is deliberately out of scope here;
  it would add edges that are not screen transitions at all.
