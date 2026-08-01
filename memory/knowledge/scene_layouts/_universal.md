# Universal chrome (visible on every screen)

The very-bottom row of the display is the same regardless of whether
the bot is on `port_overworld`, `sea`, `world_map`, inside a building,
in a sub-menu, on the main menu, etc.  Treat it as noise — these
elements are not load-bearing for game-state reasoning.

## Bottom-left — phone OS bar
- Android system status: time clock (e.g. `"22.52"`), Wi-Fi indicator
  (`"Wi-Fi"`), battery percentage (e.g. `"6.11%"`).  Driven by the
  device's OS, not the game.
- Typical position: y > 1040, x < 1700.

## Bottom-right — player UID + server name
- A long numeric/dotted string followed by the **server name** the
  player is on.  Example: `"4.0401.081.285 2604141540 Atlantic Ocean"`.
- The numeric portion is the player's UID on that server (used by
  customer support and not relevant to gameplay).
- The trailing word is the **game server**, which can be one of:
  - `Atlantic Ocean`
  - `Pacific`
  - `Indian Ocean`
  - (other server names may exist)
- Typical position: x > 1700, y > 1000.

## Hints

- Both halves of this row are **noise** for scene-state reasoning.
  When describing the current screen, **do not include the OS clock,
  Wi-Fi, battery, player UID, or server name**.
- The server name does NOT appear elsewhere in the UI — if a
  description mentions which ocean the player is sailing in, it's
  describing in-game geography (e.g. "Atlantic Ocean" as a sea region
  the ship is currently in), not the server.  These can coincide
  (Atlantic Ocean server + sailing in the Atlantic) but they're
  distinct concepts.
