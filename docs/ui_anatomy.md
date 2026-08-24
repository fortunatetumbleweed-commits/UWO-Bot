# Game UI Anatomy

Notification taxonomy, multilingual OCR rules, and port-screen layout.

## Overlay Notices vs Modal Dialogs
The game uses two distinct notification styles that the bot must handle
differently.

**Overlay notices** — translucent banners or badges that appear on top
of the current scene (port overworld, sea, building interior, anywhere)
without replacing it.  They do not open a new screen and do not block
input.  Examples:
- First-visit discovery notices (new port, village, landmark, sea region)
  — very frequent early-game
- Achievement / milestone unlocks
- Trade point rewards
- Level-up notifications
- Event start / end banners

Behaviour: overlay notices disappear on their own after a few seconds,
or can be dismissed immediately by tapping anywhere on screen.  The
underlying screen type does not change while they are visible.  The bot
should either wait briefly or tap to clear them and continue.

Special case — **region-restriction notices** (e.g. "Company LV 18
required") appear in the same style but **also halt the ship's forward
motion** until it turns out of the gated region.  Non-blocking for
input.  See `memory/project_region_restriction_notice.md`.

**Modal dialogs** — these DO replace or block the screen
(`dialog_gameplay`, `dialog_system`, `dialog_reward`, `announcement`, …)
and require explicit interaction before the bot can proceed.  Detected
by the typed `DialogModel` (see `docs/dialog_and_event_models.md`).

## Multilingual Text
The game UI language follows the user's setting (e.g. English).
However, other players are from many countries — their names, guild
tags, and chat bubbles can appear in any script: Chinese, Japanese,
Korean, Russian (Cyrillic), Arabic, etc.

**OCR rules:**
- **Safe to assume user's language**: port names (top-left), building
  names, menu labels, price tables, system UI text.
- **Must handle any script**: player names (overworld speech bubbles,
  nearby players panel, sea right-panel fleet list), guild names, chat
  messages.
- Use **PaddleOCR with the multilingual model** for any region that may
  contain player-generated text.  Latin-only OCR engines will silently
  fail on CJK / Cyrillic names.

## Port UI Overview
The port overworld is noisy: NPCs walking, speech bubbles, other
players, day / night cycle, weather (rain / snow), event banners,
floating text.  Raw OCR on the overworld is unreliable.

Key UI regions:
- **Top-left** — port name + back arrow (stable; used for OCR).
- **Right panel** (top → bottom) — season / date / time bar • mini map
  (hideable) • building list.
- **Port map** — tap the mini map → opens a clean grayscale overhead
  view of the port.  Shows every building as a labelled icon.  No NPCs,
  no weather, no chat noise.  Tapping a building on the map navigates
  the character there automatically.  Bottom-left of the port map has a
  globe icon → opens the world map.
- **Building list** — right-side scrollable list of buildings in this
  port.  Has anchor icons per entry.  Above the list: 4 tab icons
  (tasks / buildings / people / location) and the mini map.  Use only as
  a fallback — port map is preferred.
- **`?` button** — present on almost every screen; opens an in-game
  tutorial that highlights interactive elements.  Useful for first-time
  discovery of a new screen.

## Sea HUD overview
The sailing HUD contains the live ship state.  Key elements (see
`memory/project_sea_hud_latlon.md`):

- **Top-left** — current waters name (e.g. "Safe Waters", "Dangerous
  Waters") + Days of Sailing Left.
- **Top-right** — mini-map and a vertical stat column.  The
  second-from-top number in that column is the **speed in knots** (top
  speed 27 kn).  Below the mini-map is the live **lat / lon position**
  in `LL.LL,±LL.LL` format — the only direct position-truth on the sea
  HUD.
- **Right panel tabs** — one of {`tasks`, `ports`, `fleets`,
  `ship_status`}; switches contextually as nearby targets change.
- **Bottom-left** — round rudder / anchor toggle plus L / R arrow icons
  for manual steering.

## Chromed vs overworld — which signals are actually reliable

Measured 2026-08-23 across a port overworld (Banda), a market, and two village screens.

**A chromed screen has:** a title at the top-left (which is also a BACK control and names the
open sub-menu), a **menu item list on the left directly under the title**, a centre panel, a
right panel, and a top-right icon bar — *except a VILLAGE, which has no top-right icon bar*.

**Which of those can be trusted, and which cannot:**

| Signal | Reliability |
|---|---|
| **Left menu list under the title** | **Always present on chromed. Never on an overworld.** The strongest positive test. |
| **Right panel is SOLID** | Chromed — but only when the panel is showing. |
| **Right panel is TRANSLUCENT and always present** | Overworld. The scene bleeds through it. |
| Right panel *presence* | **Unreliable.** On a chromed screen the panel appears in RESPONSE to selecting an item — the village barter screen has none until a trade good is tapped. Counting panels is therefore a behaviour, not a state. |
| Home button / top-right icon bar | **Cannot separate a village from an overworld** — a village has neither, exactly like an overworld. Measured: port overworld `home=False`, market `home=True`, village landing `home=False`, village barter `home=False`. |

Measured translucency (mean horizontal detail inside the right-panel band — scene showing
through keeps it high):

    port overworld   4.59      <- translucent
    village landing  2.09      <- solid
    village barter   0.90      <- solid

**Why this matters.** With no Home button and a panel that comes and goes, nothing reliable
separated a village from a port overworld, so the verdict fell through to whichever weak
signal fired that frame. The SAME village barter screen was classified `village`, `building`,
`port_overworld`, `sea` and `unknown` within one run (2026-08-23). When it landed on
`port_overworld`, `open_world_map` tapped the calibrated port globe at (2227,361) — a
Check-Barter-Effect control on that screen — and the mission hung.

A village's left menu is `barter / explore / gifting / loot / recruit crew`; `barter` +
`gifting` together appear on no port screen.
