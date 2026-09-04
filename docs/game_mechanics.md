# Game Mechanics — UWO

Detail on the game's mechanics, growth model, and region progression.
Loaded from CLAUDE.md on demand; the project-level instructions live there.

## Game Basics
- Player runs a company with a main fleet.
- Core activities: **trade**, **exploration**, **combat** — all three grow
  the company in parallel.
- Navigation is the means to reach ports and sea regions, not a goal in itself.
- Primary currency: **ducats**; secondary: special tokens (used at specific shops).
- Growth loop: earn ducats → buy better gear / ships → unlock new routes
  → discover more → earn more.
- First visit to any port or village triggers a discovery notice — these
  must be dismissed before the bot can act.
- Ports are spread across a world map; each has a market with buy/sell prices.
- Each port has a different set of buildings; large cities have more than
  small ones.
- Building *types* (Cathedral, Bank, …) behave the same in every port;
  markets differ — different goods, different prices that fluctuate over time.

## Starting Nation & Sea Region Progression
At game start the player chooses one of five nations:
**England, Spain, Portugal, Netherlands, Ottoman**.

Home waters for each nation have no navigation restrictions — any ship can
sail there freely.

**Natural expansion ladder:**
1. **Home waters** — no restrictions (Mediterranean, North Sea, Indian Ocean
   depending on nation)
2. **West Africa / Caribbean** — accessible early; next natural step after
   home waters
3. **South Atlantic** — connects Europe ↔ Americas; moderate sea conditions
4. **Indian Ocean / East Asia** — longer voyages, requires better supply
   management
5. **Pacific** — requires high seaworthiness and momentum; very long crossing
6. **Arctic** — requires ice-breaking capability; the hardest region in the game

**Verification route plan:**
- Current: Ceylon ↔ Aceh (Indian Ocean, short hop) ✅
- Next: **Port Royal ↔ London** (Caribbean ↔ North Atlantic) — tests
  multi-hop routing, resupply stops, and longer voyage management across a
  different ocean

## Three Growth Tracks
Every action earns EXP in one or more tracks.  Levelling a track unlocks
stronger abilities.

**Adventure EXP** — union requests, discoveries (architecture, fauna,
flora, artifacts, treasures), sailing distance, entering cities.  Builds
Adventure Fame; Discovery Ranking rewards include Blue Gems.

**Trade EXP** — market transactions, requests, specialty bonuses.
- Language LV restriction removed — any market is accessible regardless
  of language level
- Mate abilities (Purchasing, Sales, Negotiation) enable better prices
- Trade-specialised ships carry more cargo

**Combat EXP** — union requests, naval victories, defeating stronger
opponents.
- PvP in Dangerous Waters; Naval Protection available
- Repel Support: instant win if fleet power ≥ 1.3× enemy

## Departure & Auto Supply (game rule)

Setting a destination from the world map raises a **Notice** dialog:

> *"Moving to \<destination\> after Auto Supply. Continue?
> Fleet will **immediately set sail** if Auto Supply is not possible.
> Sailing can be dangerous with a lack of Food and Water."*
> `Cancel` | **`OK`** — plus a "Do not show for a day" checkbox and a close X.

**Choosing OK runs Auto Supply first, which resupplies the fleet** (user, 2026-08-23). The
warning applies only to the case where Auto Supply is *not possible* — then the fleet departs
as-is, on whatever Food and Water it happens to carry. So OK is the correct answer in the
normal case, and the alarming wording is not a reason to cancel.

**Why this is written down.** It is the worked example for
`docs/one_loop_task_drives_state.md`: the decision cannot be reached from UI mechanics (the
state machine does not know a voyage exists) *or* from task context alone — "we carry ~9 days
of supply and the voyage may be longer" argues for Cancel, which is wrong. Only the game rule
settles it. That is what the reasoning / game-knowledge layer is for.

Failure it explains: on 2026-08-22 the bot mishandled this dialog, departed, and sailed with
supplies it had not topped up — the near-miss that prompted the review.

## Barter: "Insufficient" / "Depleted" are NOT blockers

Each tile in a village's **Tradable Trade Goods** row carries a stock status:

- **Insufficient** — the village is low on stock; a barter still works, it just **yields less**.
- **Depleted** — very low; a barter still works, with a **significantly reduced** amount.

Neither prevents bartering (user, 2026-08-23). They are yield modifiers, not gates, so the
bot must not treat them as a reason to stop, wait for the Stock/Negotiation Refresh timer, or
report the village as unavailable. Expect less output than the recipe's nominal figure and
carry on — which is the same trade-off already accepted for a re-rolled recipe (see
"Departure & Auto Supply" for the other case where an alarming label is not a stop signal).

Related village signals seen alongside them: a **Village Status** of "Storm" and a
**Village Stock Change** of -3%. Those explain WHY stock is low; they are not gates either.

## Barter is a QUANTITY, not a fixed-size round

The village barter panel's right side carries a quantity selector, not a progress bar
(measured 2026-08-23):

    Box of Nutmeg   Trade Quantity 220 (+25)   Total Amity Change +3,479
      [-]  [####  39/80  ....]  [grid]  [+]

- **`39/80`** — the quantity currently selected, and the maximum the panel allows.
- The game **auto-sets it to what the materials can afford**.
- The material row above Exchange shows `have / need` where **`need` is the cost of the
  CURRENT quantity**, not of a fixed recipe.

So a "round" is not a unit. Observed within one session:

    quantity 80   costs 168 / 228 / 252   output 451
    quantity 39   costs  82 / 111 / 123   output 220      <- same ratio, half the quantity

**A partial bar is still a barter.** When the materials cannot fill the bar it simply does not
reach the end, and the trade still goes through for the smaller amount (user, 2026-08-23).
Refusing to barter because "no FULL round is fundable" therefore leaves materials unspent.

**The Exchange button is the game's own verdict** on whether another barter is possible: it
greys out when one is not, having already applied the materials, the daily count and the
stock state. Prefer it over any count the bot derives — live 2026-08-23 the bot's arithmetic
said "done" while Exchange was still live, and the game was right.

Also on this panel: **Negotiate** (Quantity / Price negotiation success rates, ~13% here) and
**Check Village Influence** — neither is a commit button.

## Barter progress: the Trade Count row, and the two ways bartering ends

The bottom of the village barter panel carries **Trade Count (Current)** — the day's barter
slots, with a countdown to the reset. Measured 2026-08-23:

    Trade Count (Current)                                  (timer) 13:09:43
     [1: 476] [2: 497] [3: 451] [4: 220] [5: -] [6: -] [7: -] [8: LOCKED]

- A **filled** slot shows the OUTPUT that barter produced — here the declining yields as the
  village's stock depleted (476 -> 497 -> 451 -> 220).
- **Empty** slots are still available today.
- The last slot is padlocked (not unlocked for this fleet/village).

This is the game's own record of progress, readable at any time, and it is worth preferring
over anything the bot counts for itself.

**Bartering ends for one of two reasons, and they look different:**

| Cause | What the screen shows | Meaning |
|---|---|---|
| **Materials spent** | Exchange greys out; Trade Count still has empty slots | Done for this cargo. Leave and sell. |
| **Daily rounds used up** (all 7) | The panel returns to the village top menu on its own, and the **Barter menu item goes dark under a red "Unavailable" ribbon** | Done for today. Leave. |

**Neither is a failure.** Both mean the ship should depart (user, 2026-08-23). A stalled
Exchange tap with the materials spent is the game correctly refusing, not a fault — treating
it as one stranded a mission that had bartered four times, reporting "bartered 0 round(s)"
and never taking its route.

The `is_locked` flag from `vision.region_detectors.left_menu` is what surfaces the
"Unavailable" state.

## Amity drives BOTH the round count and the exchange ratio

Measured live at San Village, 2026-08-24, across one session:

| after | amity | per-round need | output |
|---|---|---|---|
| arrival | Neutral 60,000 | Raisin 225 + Pig 285 | 829 |
| round 1 | Favorable 66,983 | 225 + 285 | 870 |
| round 2 | Favorable 74,530 | 155 + 197 | 600 |
| round 3 | Trusting 79,733 | 3 + 3 | 9 |

Two effects, both tied to amity, and bartering itself raises amity:

1. **Round count** — every amity GRADE opens one more barter; reaching **Friendly** is where
   the daily allowance reaches **7**. The Base tab's "Daily Barter Progress N/M" shows the
   total for the grade you ARRIVE with (San read 0/5 at Neutral, Hutu 0/4 at Favorable), so
   it rises as you trade. 7 is the best hope, not a promise: if amity does not climb to
   Friendly before the available barters are spent, 7 will not happen, and that is normal.
2. **Exchange ratio** — as amity grows the ratio gets MORE FAVOURABLE: fewer materials are
   consumed per round (user, 2026-08-24). The panel rescales the trade to what is fundable,
   so late rounds cost a fraction of the first.

**Planning consequence.** Sizing a gather on the ARRIVAL ratio over-buys, because later
rounds need less — or, seen the other way, the same materials stretch to MORE rounds than
planned. San's 2-round plan produced 3 commits for exactly this reason. A gather sized on the
average ratio across rising amity would fit more rounds into the same hold, which matters
because the HOLD, not the daily allowance, is what has actually bound every plan so far
(San: 2 rounds vs an allowance of 5; Hutu: 3 vs 7).

Amity also DECAYS over time, so these gains are not permanent.

## Conditional goods — unavailable is not the same as sold out

Some goods at a port are **conditional**: purchasable only when a condition is met (user,
2026-08-24). They are NOT an empty shelf, and the difference is operational — a sold-out
shelf is restocked by a **blue-gem refresh**, while a conditional good will never restock,
so refreshing it burns gems forever.

Kinds seen so far:

| Condition | Tile appearance |
|---|---|
| **Time-limited** | GREYED, quantity 0, plus a bright **magenta band** carrying the window, e.g. `09/21/2026/00:00:00 -`, and a magenta corner flag with an hourglass. Measured at Bordeaux: Hungary Water. |
| **Nation-controlled** | Purchasable only by players whose nation controls the area — and these tiles are **ACTIVE**, not greyed. They pass every visual test and only fail at purchase. (See the frames where the bot bought Textiles.) |

So the tile states worth telling apart are three, not two:

1. **In stock** — buy it.
2. **Sold out** — greyed, dark, no condition band. A blue-gem refresh restocks it.
3. **Condition not met** — greyed WITH a condition band (time), or active but restricted
   (nation). A refresh is wasted; the good must be skipped, or sourced elsewhere.

### Reading caution

"Greyed" is not one appearance. A sold-out tile is uniformly dark grey (Raisin measured
saturation 0.004, brightness 18.5). A time-limited tile keeps a coloured backdrop behind its
grey artwork and carries the magenta band, so a naive saturation sample of the thumbnail
reads it as ACTIVE (Hungary Water measured 0.470). Sampling the tile ground, or detecting the
condition band, separates them — the artwork alone does not.

## Barter goods can be MATERIALS for other barter goods

A village's trade list mixes two roles, and the same name can hold both (user, 2026-08-24).
Read at Svear Village:

    Birch Tree     648  ← Iron 91 + Matchlock Gun 53 + Candle 105
    Näverslöjd   1,036  ← Birch Tree 260 + Iron 130          ← Birch Tree is a MATERIAL here
    Juniper Berry  926  ← Lingonberry 65 + Vodka 150
    Meteorite    1,111  ← Compass 150 + Chorong 150 + Olive Oil 150

So barter output can be barter input — at the SAME village or at another one — which makes
multi-stage chains possible: buy Iron/Matchlock/Candle → barter for Birch Tree → barter Birch
Tree (plus Iron) for Näverslöjd. **Goods that consume barter goods generally sell for more**,
which is what makes the extra stage worth it.

Two consequences for the bot:

- **Reading**: a name appearing in the list does not tell you its role. Only the row's
  structure does — flush-left qty tile with no pin = the GOOD; indented with a location pin =
  a MATERIAL. Birch Tree appears both ways on one screen.
- **Planning**: a material may be obtainable by BARTER rather than purchase, so "unsourced
  material" is not necessarily a dead end — it may be another village recipe. The gather
  planner currently only knows how to buy materials at ports.

## Multi-stage barter (barter chains)

Some barter goods are made from **other barter goods**, not from port-bought trade goods. The
chain is the game's route to its highest-value cargo — a good that consumes barter goods sells
for considerably more than one made from ordinary materials (user, 2026-08-24/25).

Confirmed chains:

    Naverslojd(769)     <- Birch Tree 340, Iron 170          Svear Village
    Moccasin(769)       <- American Bison 300, Wool 340      Cheyenne Village
    Eagle Feather(512)  <- Pulque 75, Guarana 75             Cheyenne Village

`Birch Tree` and `American Bison` are goods at the SAME village that also appear there as
materials. `Pulque` and `Guarana` are goods at OTHER villages, appearing at Cheyenne as
materials — so Eagle Feather, one of the game's high-value goods, needs two prior barters at
two other villages before it can be made at all.

### How a chained material looks on screen

Exactly like any other material: an indented row with a **location pin**. What the pin's popup
names is the difference — a **village** for a chained material, a **port** for one that is
bought. That popup is the only thing on screen that distinguishes them, and it is what a
gathering plan needs: a port material means go shopping, a village material means run another
barter first.

### What the bot cannot do yet

`memory/knowledge/barter/recipes.json` stores every material as if it were port-bought
(`source_ports`), with no field saying a material comes from a village. So a mission planning
`Moccasin` would try to BUY American Bison, and `Eagle Feather` would look purchasable when it
actually requires barters at two other villages first.

Making chains work needs, in order:

1. **Record the source kind.** Read the pin popup for each material and store village-vs-port.
2. **Plan recursively.** A village material expands into its own barter (its own materials,
   its own amity and daily-round limits at that village) before the parent good can be made.
3. **Sequence across villages.** A chain spans several villages and several days: each village
   allows a limited number of rounds per day, and amity decays, so the plan is a schedule
   rather than a shopping list.
