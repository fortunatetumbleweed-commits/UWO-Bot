# Bureau — main view

The Bureau is the administrative building where players manage city
investments, taxes, and market events.  Its main view presents the
local governance panel on the right side and three sub-menus on the
left strip.  An NPC Bureaucrat introduces available actions in the
centre.

## Layout

### Title
- Top-left: `"Bureau"` (immediately right of the back arrow).

### Left strip — Bureau's sub-menus

Three sub-menu entries:

- **Invest** — place or review investments in this city.
  *(TODO: detail panel layout not yet observed.)*
- **Tax** — configure or review tax settings.
  *(TODO: detail panel layout not yet observed.)*
- **Manage Market Event** — gated by the Mayor role.  Carries an
  `"Unavailable"` red badge when the current player is not the
  city's Mayor.  *(TODO: layout when available not yet observed.)*

### Right panel — City governance info

Three stacked rows; each row is a tappable button that opens a deeper
detail view.

#### Occupying Nation row
- Label: `"Occupying Nation"`
- Sub-label badge (when applicable): `"Allied Port"` (highlighted).
- Nation flag icon + nation name (e.g. `"Japan"`).
- Occupation percentage label (e.g. `"59.73% occupied"`).

#### Mayor row
- Label: `"Mayor"`
- Mayor's player name with guild / flag icon (e.g. `"伊達酔狂"`).
- `"Remaining Term <N>d"` info label (e.g. `"Remaining Term 3d"`).
- `"Term Week <N>"` info label (e.g. `"Term Week 1"`).

#### Market Event row
- Label: `"Market Event"`
- Event type badge (e.g. `"Bazaar"` in green).
- Trade good icon + good name (e.g. `"Liquor"`).
- Event time window (e.g. `"Apr 15d 20:00 ~ Apr 16d 21:00"`).

### Bottom-left
- **Language Effect** button — bottom-left, with a globe icon.
  *(TODO: confirm exact behavior; likely a language-skill display
  toggle.)*

### NPC presence

A Bureaucrat NPC sprite is visible centre-screen with a gold
`"Bureaucrat"` name label above the speech bubble.  Per `_default.md`,
NPC appearance varies per port — different cultures present
different administrators.  The bubble shows an introductory message
that may be truncated; treat as noise for scene-state reasoning.

## Element roles

- `back_arrow`, `home`, `building_title` (`"Bureau"`)
- `chrome_icon` — top-right cluster
- `currency_label` — top-right numeric counters
- `submenu_item` — left-strip rows (Invest, Tax, Manage Market Event)
- `status_badge` — `"Unavailable"` red badge on Manage Market Event,
  `"Allied Port"` highlighted badge on Occupying Nation row,
  `"Bazaar"` green badge on Market Event row
- `right_panel_row` (×3) — Occupying Nation, Mayor, Market Event
  (each tappable)
- `info_label` — `"Remaining Term <N>d"`, `"Term Week <N>"`,
  occupation percentage, event time window — read-only within their
  rows
- `npc_sprite`, `npc_bubble` — Bureaucrat idle content (noise)
- `action_button` — `"Language Effect"` bottom-left

## Hints

- **`"Manage Market Event"` is gated by the Mayor role.**  When the
  current player is not the city Mayor, this sub-menu entry carries an
  `"Unavailable"` red badge and cannot be entered.  The active Mayor
  is shown in the right-panel Mayor row.  See `docs/...` mayoralty
  notes for the broader mechanic: mayors rotate weekly, get the
  largest share of investor rewards, and control taxes + market
  events for their term.
- **Right-panel rows are tappable** (not merely informational).
  Tapping Occupying Nation, Mayor, or Market Event opens a deeper
  detail view.
- **Info labels embedded in row buttons** (e.g. `"Remaining Term 3d"`,
  `"Term Week 1"`, `"59.73% occupied"`) are NOT independently
  tappable — they're visual content within the tappable row.  The
  whole row is the tap target.
- **Active Market Event** shown in the Market Event row affects
  trade prices at this port for the listed good during the time
  window.  When event_type is `"Bazaar"`, that good sells higher;
  see CLAUDE.md → "Market Events" for the full type list.
- **Player UID + server name at bottom-right** (e.g. `"... Pacific
  Ocean"`) is noise — server name, NOT in-game geography.  See
  `_universal.md`.

## Source frames

- `data/sessions/2026-04-15_16-05-29/frames/0100_1619347370.png` —
  Bureau main view with all three sub-menu entries visible (Invest,
  Tax, Manage Market Event with Unavailable badge), right governance
  panel (Japan 59.73%, Mayor 伊達酔狂 with 3d remaining Term Week 1,
  Bazaar Liquor market event Apr 15d 20:00 - Apr 16d 21:00),
  Bureaucrat NPC sprite, Language Effect button.
- *(TODO: capture a clean frame with the Invest detail panel open.)*
- *(TODO: capture a frame where Manage Market Event is available.)*
