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

- **In-game date**: e.g. `Oct 12 1547`. The game world's calendar.
- **In-game time**: e.g. `23:37`. The game world's clock, NOT real-world time.

## Other always-on chrome

- **Player level**: `LV 92`. Character level.
- **Player nameplate**: small text bar above the avatar (overworld only).
- **Hamburger menu icon** (☰): top-right of port_overworld; opens main menu.

## Anti-hallucination rules

Do NOT combine chrome values into invented narratives. These are common
mistakes to AVOID:

- BAD: "current time is 23.37" followed by a story about the time of day
  influencing gameplay → the in-game clock is decoration.
- BAD: treating any 8-digit-or-larger integer as anything other than gold.

Focus your description on dynamic content: dialogs, NPC speech bubbles,
visible menu items, building entrances, action buttons, overlay banners,
and the title text at top-left of the screen.
