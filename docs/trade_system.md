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
