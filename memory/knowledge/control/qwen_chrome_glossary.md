# UWO UI Chrome — always-present elements

This glossary describes static UI furniture that appears on most or all
screens. These elements are NOT game state and should not be folded
into your scene description.

## Top bar resources (almost every screen)

- **Gold counter**: very large integer such as `16,758,149,874`. Player wealth.
- **Blue gems counter**: medium integer such as `87,866`. Premium currency.
- **Red gems counter**: smaller integer such as `3,360`. Special currency.
- **Stamina / action points**: small integer such as `691`. Energy meter.

## Top-left strip (most screens)

- **Server name**: the player's game server, almost always `Atlantic Ocean`.
  This is a server label, NOT a description of actual sailing or geography.
- **In-game date**: e.g. `Oct 12 1547`. The game world's calendar.
- **In-game time**: e.g. `23:37`. The game world's clock, NOT real-world time.

## Other always-on chrome

- **Wi-Fi / connection indicator**: small corner icon, sometimes shown as
  a percentage. Real-world network signal, NOT game state.
- **Player level**: `LV 92`. Character level. NOT a Wi-Fi value, NOT a battery.
- **Player nameplate**: small text bar above the avatar (overworld only).
- **Hamburger menu icon** (☰): top-right of port_overworld; opens main menu.

## Anti-hallucination rules

Do NOT combine chrome values into invented narratives. These are common
mistakes to AVOID:

- BAD: "Wi-Fi at 8.46% battery"  →  the `8.46%` is a connection-strength
  indicator; `LV 92` is the character level; "battery" is not in the data
  at all.
- BAD: "sailing in the Atlantic Ocean server"  →  `Atlantic Ocean` is the
  server name; the bot may not be sailing at all.
- BAD: "current time is 23.37"  followed by a story about the time of day
  influencing gameplay  →  the in-game clock is decoration.
- BAD: treating any 8-digit-or-larger integer as anything other than gold.

Focus your description on dynamic content: dialogs, NPC speech bubbles,
visible menu items, building entrances, action buttons, overlay banners,
and the title text at top-left of the screen.
