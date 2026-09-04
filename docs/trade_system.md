# Trade System

The most bot-relevant subsystem.  Loaded from CLAUDE.md on demand.

Core rule: buy below 100% price index, sell above 100%.

## Price signals
- ≤ 90% → recommended buy (shows in Recommended Purchase List)
- ≥ 110% → recommended sell (shows in Recommended Sell List)
- 7-day price trend graph with Min/Max shown on each good

## Mate abilities that improve trading
- **Purchasing** — buy-price discount
- **Sales** — sell-price surcharge
- **Negotiation** — additional market negotiation options
- **Accounting Room assignment** — shows nearby city prices on map

## Price events
Temporary boosts / crashes on individual markets:

| Event | Effect |
|---|---|
| Soaring | Price spiking up |
| Plunging | Price crashing |
| Trending | Moderate rise |
| Overflowing | Oversupply crash |
| Booming | Demand surge |

## Major Trends
Server-wide events, visible on the World Map:

- Shared budget parameter — all players spend from the same pool; depletes
  when 0% or 1 hr passes.
- After budget depletes prices stay 100–125% (guaranteed floor — safe to
  sell even after the trend ends).
- Budget size depends on concurrent players, city location, and trend type.

| Type | Cycle | Affected goods |
|---|---|---|
| Festival | 2 hr | Foods, Seasonings, Luxuries |
| Plague | 3 hr | Sundries, Medicines, Textiles |
| Flood | 5 hr | Foods, Wares, Fabrics |
| War | 7 hr | Livestock, Weapons, Firearms |
| Sponsor | 11 hr | Dyes, Crafts, Artworks |
| Development | 13 hr | Wares, Ores, Liquors / Foods |
| Boom | 17 hr | Metals, Artworks, Jewelries |
| Extravagance | 19 hr | Crafts, Spices, Perfumes |

Example prices during trends: Foods 299–319% (Festival), Metals 151–171%
(Boom), Ginseng (Medicines specialty) 242–262% (Plague).

## Trade Points
- Accumulate per Trade Area when profits exceed threshold.
- 1 000 points → random reward based on Company Level.
- Rest Points provide multipliers for lower daily profits (applied after
  midnight).

## Sales Sets
Selling specific good combinations triggers a multiplier; changes weekly
per market.  Sales Set UI elements:

1. difficulty
2. multiplier amount
3. achieved (green)
4. required goods + qty
5. profit thresholds
6. total multiplier
7. Rest Points
8. total Trade Points
9. max multiplier limit (increases with Trade Strategy Expertise)

## Tax
Applied to foreign-nation markets after Beginner Benefits end.  Exemptions:
- Tax Permits (bought with Blue Gems at the nation capital Palace; have
  expiry dates)
- National Reputation
- Admiral titles
- City traits
- Expertise bonuses

## Market Events (mayor-controlled)
Set from Bureau → *Manage Market Event*, 1 hr duration, consume currency:

- **Urg. Buy** — selected goods sell higher
- **Urg. Sale** — selected goods purchase lower
- **Bazaar** — selected trade good *type* sells higher
- **Closeout** — selected trade good *type* purchases lower
- Consecutive mayoral terms unlock additional event types and more good
  designations.

### Selling into a Bazaar — measured flow (Bremen, 2026-08-24)

First live event sale: **1,644 Box of Nutmeg at 211% → +595,535,712 ducats.**

**Reading the schedule.** World map → *Trade Event Schedule* (bottom-left). The table is
`Market Event | Trade Goods | Fixed-term | Location`, read column-anchored by
`vision/trade_event_reader.py`. **Times are Korean (UTC+9)** — not local, not in-world — so
everything is handled timezone-aware. The list scrolls; a clipped row comes back with
`start=None` rather than an invented time.

**Getting there.** Each row has two right-edge buttons: scales (trade info) and the
**location pin**. The pin does *not* sail — it opens a **Location Info** dialog (city,
`Nearby` badge, mini-map) and its gold **Move** button commits the voyage. On arrival the
player walks to the market unaided, so no world-map search and no saved route are needed.

**Confirming departure.** Read the **speed**, which the game prints in knots in the tile-strip
left of the mini-map: `>0` is under way, `0.0` is the speed-0 bug. This is the direct answer
and confirms departure immediately, rather than waiting a game-day for the older test (falling
ETA / rising day-at-sea) — which cannot answer these hops at all, since a 1-day ETA has no
finer granularity to fall through and the trip is over in ~90 s.

Two caveats, both learned live: `read_speed` needs `locate=True`, because its fixed crop is an
offset from a `MINIMAP_CROP` constant the UI has drifted ~120px away from (the mini-map's real
left edge measured x≈1862 against a constant saying 1984, so the crop landed inside the disc
and returned None on every at-sea frame). And an unreadable speed is *unknown*, not stopped —
it falls through to the ETA test, with **arrival** as the final confirmation. A `0.0` read
immediately after the tap is not a verdict either; the ship may still be accelerating.

The band is located from the mini-map rather than hardcoded, and is deliberately generous:
**speed is the only decimal in that strip** (wind and current are integers), so the
`\d{1,2}\.\d{1,2}` pattern disambiguates it without needing precise geometry.

**Confirming the bazaar before selling.** Two cues on the Sell tile, both measured:
- a **green banner carrying the literal word `Bazaar`** — machine-readable, so no colour
  threshold needs calibrating;
- a **price index far above normal**: 211% against the 96-99% of the ordinary goods beside
  it. `BAZAAR_MIN_INDEX = 150` gates the sale.

Sell **only** that category. The rest of the hold is at ordinary pricing and is often barter
materials the mission still needs.

**Perception gotcha.** A tile wearing the banner is detected *clipped* — OmniParser returns
432x181 where its neighbours are 435x231. The width is dead-on; only the height is short, and
the index text falls *below* the clipped box. Both the grid's congruence filter and the
cell's text pickup allow for this (`vision/grid_detector.py`, `vision/market_reader.py`), and
the grid keeps one cell per slot so the clipped twin of a normal tile is not read twice.
