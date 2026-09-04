# Activity capabilities — design draft and registry table

**Status: DRAFT FOR REVIEW, 2026-08-30.** Extends
`docs/activity_capability_registry.md` (the argument) with the concrete table (the content).
Companion to `docs/intent_graph.md`, which answers the other half — how to REACH a place.

## The model in one page

Three kinds of thing, currently conflated in one `SERVES` tuple per activity:

| kind | test | leaving it | examples |
|---|---|---|---|
| **WORLD** | has the ☰; Back raises "Exit Game?" | only by travelling | `port_overworld` · `sea` |
| **CHROMED SCREEN** | title bar IS the back button; no ☰ | Back, one level | `village` · `building:*` · `sub_menu:*` |
| **CONTEXT** | drawn OVER something that stays visible | Back, or its own close | `main_menu` · City Info · the barter panel |

A CAPABILITY is work that can be done at a place. A capability is not a screen: several
capabilities share one screen, and one capability (`ReadHold`) is reached through a context
that two worlds both host.

**On a chromed screen the capabilities are written on it.** The left menu IS the list — this
is why villages and buildings are easy and need no invention:

```
village   left menu reads:  Explore · Gifting · Loot · Recruit Crew · Barter
market    left menu reads:  Purchase · Sell
port      building list:    Harbor · Market · Shipyard · Bank · Inn · Cathedral ·
                            Item Shop · Union · Bureau / Mercator Estate  (varies by port)
```

`_VILLAGE_MENU_WORDS = ("barter", "gifting", "loot", "recruit")` already exists in
`brain/village_context.py` — as a *classifier* for "is this a village". The same words are
the capability list; nothing reads them as one.

## The registry table — FOR REVIEW

Confidence is marked because the point of this draft is to review the uncertain rows, not to
assert them.

### Chromed screens — clear, read from the menu

| screen | capabilities | today's goals | confidence |
|---|---|---|---|
| `village` | barter · gift · loot · recruit crew · read the trade list | `Barter`, `RemoteCheck` | **high** — the menu says so |
| `building:market` / `sub_menu:purchase` / `sub_menu:sell` | buy · sell · trim to a target | `Hold`, `FreeHold`, `TrimHold`, `SellHold` | **high** |
| `building:harbor` | depart · supply departure · recruit crew | `Depart`, `RecruitCrew` | **high** |
| other buildings (shipyard, bank, inn, cathedral, item shop, union) | — none used yet | — | n/a |

### Worlds — need review

| screen | capabilities | notes | confidence |
|---|---|---|---|
| `port_overworld` | enter a building · open the world map · **read the hold (via `main_menu`)** | the building list is the capability list, and it VARIES BY PORT — Amsterdam has Mercator Estate, Tripoli has Sanctuary/Bureau. So this row is per-port, not global. | **medium** |
| `sea` | open the world map (minimap) · **read the hold (via `main_menu`)** · steer manually | steering is out of scope for the mission task but is a real capability | **medium** |

### Contexts — need review, and the interesting cases

| context | host(s) | capabilities | confidence |
|---|---|---|---|
| `main_menu` | `port_overworld` + `sea` | read the fleet (capacity, cargo, water, food) | **high** — identical in both, one reader |
| `location_info` / `village_info` | `world_map` | commit a course (the gold Move button) | **high** — proven live |
| `village_info_base` / `_barter` | `world_map` | read amity, rounds, the trade list REMOTELY | **high** |
| `destination_list` / `destination_panel` | `world_map` | choose a destination · commit it | **high** |
| `event_schedule` | `world_map` | read the trade event schedule | **high** |
| `route_list` | `world_map` | take a saved route | **low** — never exercised |
| the barter panel | `village` | select a good · exchange · read amity and materials | **high** |

### The world map — the hardest row

The world map is a WORLD by the ☰ test (it has none) and a CHROMED SCREEN by the back test
(the title is a back button), yet it behaves like neither: it is the only place a course can
be set, and nearly all of its capability lives in CONTEXTS rather than on the map itself.

`world_map_context` already enumerates eight of them. So the honest reading is that the world
map is a chromed screen whose capabilities are almost entirely contextual — and the registry
should say so rather than list "choose a destination" as if the map offered it directly. The
map itself offers: pan, zoom, tap a pin, open a rail, open the event schedule. Everything
else is what the resulting context offers.

## The Android parallel — intent filters (user, 2026-08-30)

> *"for an Android activity you can register what intent it listens to, this capability
> registration is a bit like that."*

Principle #1 already borrows Android's split between intent dispatch and input dispatch. This
borrows the layer under it — **resolution**:

| Android | here |
|---|---|
| `<intent-filter>` declared in the manifest | an activity's declared capabilities |
| an implicit intent resolved to an activity | a work order resolved to an activity |
| `PackageManager.queryIntentActivities()` — ask BEFORE sending | what the dispatcher needs and does not have |
| **`ActivityNotFoundException`** | "an undispatched work order is a defect, and is logged" |

The last row is the strongest argument for the rule. Android **throws** when nothing matches
an implicit intent: a named, loud failure at the moment of resolution. Our dispatcher sends
`ENTER_BUILDING` at a village, changes nothing, and waits for a stall guard to infer six
ticks later that something was wrong. Android treated that as an error decades ago.

### Where the analogy BREAKS, and why it matters

**Android resolution is global.** `startActivity` can launch any matching activity from
anywhere; the system creates it. Ours cannot — the fleet has to physically BE somewhere. So
what Android does in one step, this design needs two:

1. **Who can do this work?** — the capability registry (the intent filter)
2. **Can it be done FROM HERE, or must we travel first?** — `docs/intent_graph.md`, which has
   no Android counterpart because launching is free there

Every wedge on 2026-08-29 was those two collapsed into one. `OPEN_WORLD_MAP` at a village is
not "nothing handles this" — `WorldMapActivity` handles it perfectly. The fleet simply was not
on the map. **Resolution succeeded and reachability failed**, and nothing in the code could
tell those apart, so the failure had no name and no owner.

### One detail worth stealing

Intent filters live in the MANIFEST: static, declared, queryable without running the
activity. That is a direct answer to the open question below about declaring capabilities
versus reading them off the left menu. A manifest can be consulted BEFORE committing to
anything; the menu can only be read while already standing there. Android has both kinds of
knowledge and deliberately resolves against the declaration — which suggests: declare the
capabilities, and let the menu read VERIFY them (and teach new ones), rather than being the
only source.

## What the registry is FOR

1. **The dispatcher refuses work a place cannot do**, instead of dispatching into a void and
   waiting for the no-progress guard six ticks later.
2. **An undispatched work order is LOGGED as a defect** — naming the order and the activity
   that refused it. Every wedge on 2026-08-29 was this event happening silently.
3. **Support becomes opportunistic.** The task is gather · barter · sell. Trim, supply checks
   and capacity reads are SUPPORT: taken when the place affords them, never scheduled. That
   removes `_ASHORE_ONLY`, `_NO_MARKET`, the optional-leg skip, and most of
   `_departed_for_the_village`.

## Open questions for the review

- **Per-port capability.** The building list varies (Mercator Estate vs Sanctuary vs Union).
  Is `port_overworld` one row with a per-port set, or is the KB the registry for that?
- **A third state: refused.** Frame 15 of run 11 shows the village's Barter item present and
  wearing a red `Cannot Exchange` ribbon — the day's rounds spent. Capability is not
  can/cannot but can · cannot · **can-but-refused-now**, and only the screen knows which.
- **Is `supply_verify` support or a precondition?** It gates a voyage, which is not
  opportunistic — support that must happen before a leg is just a leg with better manners.
- **Where do capabilities come from?** Declared in code, or READ from the menu each time?
  The menu is on screen, which argues for reading it — and would make a new building work
  without a code change (cf. `docs/affordance_learning_and_curiosity.md`).
