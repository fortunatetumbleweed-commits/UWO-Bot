# Low stock is a PLANNING input, not something to grind through

**Status: DESIGN, not built** (2026-09-06). Recorded from a live run; the code change waits.

## What happens today

The plan picks gather ports by COVERAGE — which port sells the most of what we need — and
never by yield. So it will pick a drained shelf and refresh it, over and over, because the
refresh always "works":

    Faro    ~457 Pig per refresh cycle
    Madeira ~110 Raisin per refresh cycle      <- same 11 blue gems, a quarter of the goods

Live 2026-09-06, the Hutu run: Raisin needed 1,755 and arrived 110 at a time. Ten refreshes
got it to 1,100 across ~45 minutes, and the leg ended SHORT — capping the barter at 5 rounds
against a planned 7. Faro's Pig, four times the yield per gem, was met in three.

The user's framing: *"if the bot sees a port is at low stock, it records the data and replans
immediately, so the whole process will be faster. If all ports have low stock, then just
abandon the task as non-profitable for the season."*

## The signal, measured

The game already says it, in the tile's TOP-RIGHT corner — a coloured ribbon carrying a
flower icon. This is `memory/stock-status-is-a-colour` ("tile CORNERS carry it: top-right
seasonal stock, top-left guild monopoly"), and it is the same shape as the existing
`_tile_has_condition_ribbon`, which reads the top-LEFT corner for a guild lock.

Measured on the Madeira Purchase grid, frame 158 of
`data/sessions/trace_barter_cmd_2026-09-06T21-45-01`, over a 60x52 band at the tile's
top-right:

| tile | red | green | reading |
|---|---|---|---|
| Madeira Wine | 0.000 | 0.000 | normal |
| Sugar | 0.000 | 0.000 | normal |
| Keris | 0.000 | 0.000 | normal |
| Wooden Statue | 0.000 | 0.000 | normal |
| **Sugar Cane** | **0.447** | 0.000 | **LOW** |
| **Shea Butter** | 0.000 | **0.469** | **ABUNDANT** |
| Raisin | 0.138 | 0.000 | see below |

A clean separation for the two ribbons — 0.45 against 0.00 — so the threshold is not
delicate, exactly as the magenta ribbon's 62%-vs-0% was not.

**Raisin's 0.138 is NOT a ribbon and must not be read as one.** The tile carries no visible
ribbon; it is SOLD OUT, and the reading comes from the stamp and the red `0`. So the detector
must be built and checked against a sold-out tile, or it will report "low season" for every
empty shelf — a different fact with a different remedy (a refresh DOES restock an empty shelf;
it does not undo a bad season).

A SECOND SIGNAL EXISTS and is not yet measured: the quantity badge itself is coloured — white
normally (Madeira Wine 136, Sugar 232, Keris 118, Wooden Statue 143), RED when low (Sugar Cane
339, Raisin 0), GREEN when abundant (Shea Butter 342). Visible to the eye; a first attempt to
measure it cropped the wrong band and returned nothing, so it needs doing properly. Two
independent signals agreeing would be worth more than either alone — this codebase already
prefers that (`memory/constraints-beat-votes`).

## What to record

A good's season is a PLACE fact with a long life, so it belongs in the market KB
(`memory/market_kb.py`, one JSON per port) rather than in any activity:

    low_stock: {good: {"seen_at": <epoch>, "state": "low" | "abundant"}}

**Expiry: 72 hours** for now (user: *"I am not exactly sure but I think it is 3 game months"*)
— a guess, so it must be recorded as one, in the constant's name and comment, and revisited
once a season boundary has actually been observed. An entry past its expiry is DROPPED, never
trusted: a season that has turned makes the record a conclusion whose evidence is gone
(CLAUDE.md #2).

`MarketGood` gains the observation, alongside the `conditional` flag it already carries.

## What to do with it

1. **Seeing low stock replans immediately.** Not "finish this leg, then reconsider" — the
   whole point is to stop paying gems at a quarter rate. The gather leg reports the season
   and the plan picks another port for that material.
2. **Ranking prefers a port with no low-stock record** for the materials it covers. This is
   the pending `Rank gather ports by units-per-gem` task, and the season flag is the input it
   was missing.
3. **Every candidate port low ⇒ abandon the task**, reported as *not profitable this season*
   rather than failed. That is a real answer about the world, and it is the one case where
   stopping early is correct rather than a give-up.

## Why this is not just an optimisation

A refresh that yields 110 units is not a failure — nothing errors, the gem is spent, the tile
goes active, and the loop is happy to repeat. Without the season flag the bot cannot tell a
port that is merely slow from one that is drained, so it grinds. The signal to tell them apart
has been on screen the whole time.

## The other half: buy in BALANCE, not to each material's own target

The same run made the cost of ignoring this exact:

| | bought | used (6 rounds) | left |
|---|---|---|---|
| Pig | 1,828 | 1,099 | **729** |
| Raisin | 1,100 | 1,099 | 1 |

A round consumes ALL its materials, so the barter is capped by the SCARCEST one. Raisin capped
it at 6 rounds; every Pig bought past ~1,100 was dead weight — money, gems, hold space, and
the Faro refreshes that fetched it. The plan bought each material to its own padded target
(1,755) as though they were independent.

The rule: **buy to the number of ROUNDS that can actually be funded.** Once Raisin is known to
cap at 6, Pig's target is 6 rounds' worth and its leg should stop there. This is the pending
`Stop buying when the rounds are funded, not when the padded goal is hit` task, and it is the
same planning gap as the season flag above — both are the plan refusing to revise itself while
the world is telling it something.

Together they compound: the season flag says "Raisin will be slow here", the balance rule says
"then do not buy 1,828 Pig", and the replan says "and try Bordeaux for the Raisin". Any one
alone leaves most of the waste in place.
