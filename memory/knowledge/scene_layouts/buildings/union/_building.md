# Union — main view

The Union is the building where the player picks up and manages
requests (quests / contracts offered by various factions or NPCs).
Its main view lands directly on the request board; there is no
secondary sub-menu strip visible in this frame.

## Layout

### Title
- Top-left: `"Union"` (immediately right of the back arrow).

### Centre area — request board

The primary interactive area occupies the centre of the screen.  In
this frame two labelled elements are detected here:

- **`Requests` entry-point** — a tappable element in the upper-centre
  area of the board.  The detector also sees the labels `"Unavailable"`
  and `"Limited"` in close proximity, suggesting status badges that
  qualify the request list.
  - `"Unavailable"` appears as a `status_badge` (red pill style,
    observed top-left of the entry-point cluster).
  - `"Limited"` appears as a subordinate label beneath or alongside
    `"Unavailable"`.  *(TODO: confirm whether `"Limited"` is a second
    status badge or a sub-category row label.)*
- **Red notification dot** on the `Requests` row label — indicates at
  least one new or pending request is available.  *(TODO: confirm
    whether the dot is a count badge or a simple "new" indicator.)*
- A small `icon` element is detected at the left edge of the
  centre area, vertically aligned with the NPC speech bubble region.
  *(TODO: confirm what this icon represents — possibly a faction
  emblem or request-category indicator.)*

### NPC presence

A building-owner NPC sprite is visible centre-screen as ambient idle
content.

### Bottom row

- **`Language Effect`** button at the bottom-left corner.  *(TODO:
  confirm exact behavior; consistent with other building views where
  this appears as a language-skill bonus indicator.)*

## Element roles

- `back_arrow`, `home`, `building_title` (`"Union"`)
- `currency_label` — top-right numeric counters (ducats `3,548,741`,
  gems `5,952`, a third counter `408` *(TODO: confirm resource type)*)
- `chrome_icon` — top-right cluster (two icons: likely mail / settings)
- `tappable_row` — `"Requests"` centre board entry
- `status_badge` — `"Unavailable"` red pill adjacent to `Requests`
- `status_badge` (or `info_label`) — `"Limited"` label adjacent to
  `Requests`  *(TODO: confirm tappability)*
- `icon` — unidentified icon at centre-left of the board area
  *(TODO: confirm role)*
- `npc_sprite`, `npc_bubble` — Union Master idle content (noise)
- `button` — `"Language Effect"` bottom-left

## Hints

- **`"Unavailable"` badge on `Requests`** — suggests some requests in
  the current list cannot be accepted (possibly level, rank, or
  prerequisite gating).  The bot should not assume all displayed
  requests are actionable.
- **`"Limited"` label** — may indicate a cap on how many requests can
  be held simultaneously, or that only a subset of request types is
  available at this port.  *(TODO: observe with different game state
  to confirm meaning.)*
- **Red notification dot on `Requests`** — reliable signal that there
  is at least one new request to inspect; the bot can use this as a
  trigger to open the request list.
- **`Language Effect` button** is present at bottom-left as in other
  building views; treat as chrome — not part of the Union-specific
  interaction flow.
- **No sub-menu strip is visible** in this frame.  The Union main view
  appears to be a single-screen layout with the request board as the
  sole primary content area.  *(TODO: verify whether additional
  sub-menus exist that were not visible in this frame — e.g. a history
  or faction-standing panel.)*
- **NPC speech bubble text is noise** — it varies by context and
  should not be used for scene-state reasoning.

## Source frames

- `data/sessions/2026-04-15_16-05-29/frames/0058_1613348033.png` —
  Union main view with `Requests` row showing `Unavailable` /
  `Limited` badges and a red notification dot; `Language Effect`
  button bottom-left; NPC sprite centre-screen.
- *(TODO: capture a frame with the `Requests` list fully open to
  document the request-list detail panel layout.)*
- *(TODO: capture a frame where `Unavailable` is absent to confirm
  the badge is conditional on game state.)*
- *(TODO: capture frames showing any additional sub-menus or panels
  that may exist within the Union building.)*
