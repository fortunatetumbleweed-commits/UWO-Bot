# Union — Requests sub-menu

The Requests sub-menu shows available quest-style tasks issued by the Union.  Players can accept requests from the central list; the right panel provides contextual information about how requests work and tracks the player's active / available request slots.

## Layout

### Section header — Request in Progress

- A `"Request in Progress"` label spans the top of the main content area (left-centre zone), acting as a section title for the scrollable request list below it.
- To the right of this label sits a **refresh-style icon** (circular arrow) and a small **cost indicator** showing a gem icon followed by `"10"` — likely the cost to refresh the available request list.  *(TODO: confirm whether the refresh icon is tappable and whether `10` is the gem cost per refresh.)*

### Left strip badge — Unavailable / Limited

- A two-line status badge in the upper-left of the main content area reads `"Unavailable"` (top line, styled as a coloured label) and `"+ Limited"` (bottom line).  *(TODO: confirm what `Unavailable` / `Limited` means in this context — possibly indicates that limited-edition requests are currently not accessible on this account/level.)*

### Request list — centre area

Each row in the request list represents one available request.  Two rows are visible in this frame:

**Row 1 — "Basics of Adventure Chapter 1"**
- **Rarity badge**: `"Rare"` (purple/violet pill, top-left corner of the row).
- **Row icon**: a compass-style circular icon to the left of the text block.
- **Title**: `"Basics of Adventure Chapter 1"`.
- **Sub-label / objective**: `"Discover new Discovery"`.
- **Accept button**: gold `"Accept"` button on the right edge of the row.

**Row 2 — "Basics of Combat Chapter 1"**
- **Rarity badge**: `"Rare"` (purple/violet pill).
- **Row icon**: compass-style circular icon (same style as Row 1).
- **Title**: `"Basics of Combat Chapter 1"`.
- **Sub-label / objective**: `"Use Artillery during a Naval Combat"`.
- **Accept button**: gold `"Accept"` button on the right edge of the row.

*(TODO: confirm maximum number of requests visible at once; the list may be scrollable if more are available.)*

### Right panel — Requests information + slot list

The right panel (approximately the rightmost quarter of the screen) is divided into two vertical zones:

#### Upper zone — explanatory text block

A descriptive text block (non-tappable) explains the request system.  Observed text (reproduced for indexing; not for state reasoning):

> "Currency, Fame, and EXP can be obtained upon completing requests from Union.  Requests with the same name and reward as the one just completed will not reappear."

This is a static `info_label` / `text` block; it is not interactive.

#### Lower zone — request slots

Three numbered slot rows are visible, stacked vertically:

| Slot | Label / State | Cost indicator |
|------|---------------|----------------|
| **1** | `"No request in progress."` | — (no cost shown) |
| **2** | `"Can use for 7 days"` | gem icon + `"200"` |
| **3** | `"🔒 Company LV 15"` (locked, red/orange background) | — |

- **Slot 1**: Active (or empty) slot.  Currently shows `"No request in progress."` — indicates no request is currently accepted into this slot.
- **Slot 2**: Available slot, shown with a duration label `"Can use for 7 days"` and a cost of `200` gems to unlock or extend.  *(TODO: confirm whether this slot must be purchased before it can hold a request, or whether the `200` is a different action cost.)*
- **Slot 3**: Locked slot.  Displayed with a red/orange fill and a lock icon, with the unlock condition `"Company LV 15"`.  Not currently tappable in a meaningful way (locked state).

### Bottom-left

- A `"Language Effect"` button is present at the bottom-left.  Per `_default.md` convention, this is shared chrome; see the default building layout notes.

## Element roles

- `text` / `panel_title` — `"Request in Progress"` section header above the request list
- `icon` + `button` — refresh icon and `"10"` gem cost indicator (top-right of the request list header)
- `status_badge` — `"Unavailable"` / `"+ Limited"` label stack in the upper-left content area
- `tappable_row` — each request row (`"Basics of Adventure Chapter 1"`, `"Basics of Combat Chapter 1"`)
- `status_badge` — `"Rare"` pill on each request row
- `icon` — compass icon on each request row
- `info_label` — objective sub-label on each request row (`"Discover new Discovery"`, `"Use Artillery during a Naval Combat"`)
- `action_button` — gold `"Accept"` button on the right edge of each request row
- `panel_title` — `"Requests"` heading at the top of the right panel
- `text` / `info_label` — explanatory text block in the upper right panel (non-tappable)
- `right_panel_row` — numbered slot rows 1, 2, 3 in the lower right panel
- `status_message` — `"No request in progress."` in slot 1
- `info_label` — `"Can use for 7 days"` in slot 2
- `currency_label` / `icon` — `200` gem cost indicator in slot 2
- `locked_row` *(proposed new role)* — slot 3 with lock icon and `"Company LV 15"` condition, red/orange background indicating inaccessible state
- `chrome_icon` — top-right cluster (currency / settings / mail icons)
- `currency_label` — `"3,548,741"` (gold) and `"5,952"` (gems) top-right

## Hints

- **Accept buttons are per-row** — each request row has its own `"Accept"` gold button.  The bot must tap the correct row's button, not a global accept.
- **Slot capacity is gated** — slot 2 requires a gem purchase (`200` gems), and slot 3 requires `Company LV 15`.  If the bot needs to queue multiple requests simultaneously it must verify available unlocked slots first.
- **`"No request in progress."` in slot 1 is a status message, not a button** — it visually occupies the slot area but is not interactive.
- **Slot 3 locked row is not tappable** (or tapping it will likely show a lock/upgrade prompt rather than accepting a request).  *(TODO: capture a frame where slot 3 is tapped to confirm behavior.)*
- **The `"Unavailable / + Limited"` badge** appears to apply to the overall limited-request category, not to the individual listed requests.  The listed `"Rare"` requests each have their own `"Accept"` button and appear available.  *(TODO: clarify relationship between the `Unavailable / Limited` badge and the request list.)*
- **Refresh cost** of `10` gems (top of request list) likely refreshes the pool of shown requests.  Completed requests with the same name/reward will not reappear (per on-screen text).
- **`"Rare"` rarity badge** is a visual indicator only, not tappable.

## Source frames

- `data/sessions/2026-04-15_16-05-29/frames/0059_1613402342.png` — Requests sub-menu with two Rare requests visible (`Basics of Adventure Chapter 1`, `Basics of Combat Chapter 1`), slot 1 empty, slot 2 available for 200 gems, slot 3 locked at Company LV 15.
- *(TODO: capture a frame with an active request in slot 1 to document the in-progress row state.)*
- *(TODO: capture a frame after accepting a request to confirm row / slot state changes.)*
- *(TODO: capture a frame with slot 3 tapped to confirm the locked-state interaction.)*
- *(TODO: capture a frame with more than two requests in the list to confirm scrollability.)*
