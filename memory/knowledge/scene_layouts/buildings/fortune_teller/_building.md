# Fortune Teller — main view

The Fortune Teller building provides access to fortune readings, contracts, blueprints, and tools.  Its main view presents a left-strip sub-menu list; no secondary detail panel is shown by default on the right side.

## Layout

### Title

- Top-left: `"Fortune Teller"` (immediately right of the back arrow).

### Left strip — Fortune Teller's sub-menus

Observed sub-menu options in the left strip (each row is tappable; prefix `+` indicates an alternate sub-menu the player can switch to):

- **Fortune** — *(TODO: detail panel layout not yet observed.)*
- **Contract** — *(TODO: detail panel layout not yet observed.)*
- **Blueprint** — *(TODO: detail panel layout not yet observed.)*
- **Tool** — *(TODO: detail panel layout not yet observed.)*

*(TODO: confirm whether these are the complete set of sub-menus or if additional options appear under certain conditions.)*

### Centre / Right area

In this observed frame, no detail panel is expanded on the right side.  The centre of the screen is occupied by the NPC sprite and ambient idle content (see NPC presence below).

A single `icon` element is detected near centre-screen at approximately mid-height; its function is unclear from this frame.  *(TODO: confirm whether this icon is a tappable element or part of the NPC sprite's ambient decoration.)*

### Bottom row

- **Language Effect** button at the bottom-left corner.  *(TODO: confirm exact behavior; likely a language-skill bonus indicator shared with other buildings.)*

### NPC presence

A Fortune Teller NPC sprite is visible centre-screen as ambient idle content, accompanied by a speech bubble.  Per `_default.md`, NPC appearance varies per port and is noise for scene-state reasoning.

## Element roles

- `back_arrow`, `home`, `building_title` (`"Fortune Teller"`) — top chrome shared with all buildings (see `_default.md`)
- `chrome_icon` — top-right cluster (settings / mail icons; numeric badge `408` on the settings/gear icon)
- `currency_label` — numeric top-right counter (e.g. `5,952` gems visible; additional currency counters present per top-right chrome)
- `submenu_item` — left-strip rows (`Fortune`, `Contract`, `Blueprint`, `Tool`)
- `icon` — unidentified icon near centre-screen *(TODO: confirm role)*
- `npc_sprite`, `npc_bubble` — Fortune Teller NPC idle content (noise)
- `button` — `"Language Effect"` at bottom-left corner

## Hints

- **No default detail panel**: unlike the Harbor (which shows a Departure panel immediately), the Fortune Teller's main view does not display a right-side detail panel until the player taps one of the left-strip sub-menus.  *(TODO: verify — the detected button element spanning the full label block may indicate a collapsed or overlay panel rather than a true absence of a detail panel.)*
- **Sub-menu rows are the only tappable content** visible in this frame beyond the standard chrome and Language Effect button.
- **Left-strip sub-menu labels** are prefixed with `+` in the UI, consistent with the convention used in other buildings.
- **Language Effect button** appears to be present in multiple building interiors — its behavior should be documented centrally if it is universal.
- **Centre icon** of unknown role: OmniParser detected a standalone `icon` element at approximately mid-screen.  It may be an overlay decoration on the NPC sprite or a hidden interactive element.  Do not attempt to tap it without further investigation.

## Source frames

- `data/sessions/2026-04-15_16-05-29/frames/0110_1620289972.png` — Fortune Teller main view, no sub-menu selected, NPC sprite and bubble visible centre-screen, left strip showing Fortune / Contract / Blueprint / Tool.
- *(TODO: capture frames for each sub-menu detail panel: Fortune, Contract, Blueprint, Tool.)*
- *(TODO: confirm the role and tappability of the unidentified centre icon element.)*
- *(TODO: confirm whether the full sub-menu list is always exactly these four items or varies by port / player progression.)*
