# Exploration, Fishing, and Port Investment

Loaded from CLAUDE.md on demand.

## Exploration
- **Sea Exploration**: use spyglass from ship.
- **Land Exploration**: anchor near land; has regional traits, min days,
  tool selection.
- **Villages**: recruit crew or gather supplies via gifting (ducats) or
  looting.

## Fishing
Separate mini-game mode:
- Requires Fishing Mode on a sailing ship.
- Energy system: 1 energy per 5 attempts; auto-fishing available.
- Equipment: rod + paste, 5 tiers (Cheap → Legendary); paste lasts 90 sec.

## Port Investment System
Ports have a weekly investment leaderboard.  Players and guilds invest
ducats into a port to gain ranking rewards and influence.

**Investor rewards** (distributed weekly based on ranking):
- Blue gems — premium currency usable across many game systems
- Ducats — direct cash return
- Investment chests — contain blue gems, investment season tokens, and
  other items
- Investment season tokens — spent at the current season's shop for
  exclusive items

**Mayor** — the player with the highest investment in the previous week:
- Rotates every week
- Can raise or lower taxes for specific countries (affects all traders at
  that port)
- Can set market events (temporary price changes, special goods
  availability)
- Receives the largest share of weekly rewards

**Bot strategy implications:**
- Investing in ports the bot trades through regularly generates passive
  blue gem / ducat income.
- Mayor tax changes affect trade profitability — the KB should track
  current tax rates per port.
- When scouting a new port, the city overlay (`port_arrival_overlay`)
  shows current mayor and top investors — useful for assessing how
  competitive a port's investment scene is.
- Investment is done at the **Bureau** — present at every port, same as
  any other building.
- Long-term: owning mayoralty on key trade ports gives price-control
  advantage.
