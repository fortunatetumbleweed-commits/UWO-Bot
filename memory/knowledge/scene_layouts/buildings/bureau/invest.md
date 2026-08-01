# Bureau — Invest sub-menu

The Invest sub-menu shows a ranked leaderboard of private investors
(and tabs for Nation / Guild / Guild Only investors), a right-side panel
for investing in Industry / Commerce / Military, and an Investment Point
Goal tracker.  Dividends and rewards are distributed on a weekly cycle.

## Layout

### Top tab bar — investor scope selector

Four `mode_tab` buttons span the top of the main content area:

- **Private** (selected / active by default in this frame)
- **Nation**
- **Guild**
- **Guild Only**

Tapping a tab switches the leaderboard below to show investors of that
scope.

### Column headers (below tab bar)

Three read-only column header labels span the leaderboard:

- **Investment Point** — centre column
- **Tax Perks** — right of Investment Point
- **Reward** — far right

These are `info_label` elements; they are not tappable.

### Left strip — Bureau sub-menu items (reference only)

- **Tax** — visible as a sub-menu item in the left strip.
- **Manage Market Event** — visible below Tax; shown with an
  `"Unavailable"` `status_badge` in this frame, indicating it is
  currently locked or inaccessible.

These are documented in `buildings/bureau/_building.md`.  Noted here
only because the `Unavailable` badge is visible and relevant to
navigation decisions.

### Leaderboard — ranked investor rows

Each row is a `tappable_row` representing one investor/company.  Observed
rows (Private tab active):

| Rank | Name | Investment Points | Dividend % | Reward icons |
|------|------|-------------------|-----------|--------------|
| 1 | 伊達酔狂 *(with appellation badge)* | 10,124,319 P | 5% | three reward icons |
| 2 | 李华梅舰队 | 969,950 P | 4% | three reward icons |
| 3 | FoxFleet | 609,045 P | 4% | three reward icons |
| 4 | 阿杰哥挺了 | 563,923 P | 4% | three reward icons |
| 5 | 最终皇帝 *(partially visible)* | *(obscured)* | *(obscured)* | *(obscured)* |
| - | Tumbler *(current player's own entry)* | 0 P | — | — |

- Rank 1 entry carries a small `status_badge` / `appellation` indicator
  (orange badge, labelled `"1주"` or similar — *(TODO: confirm exact
  meaning; likely "1 week" duration badge or rank-streak indicator)*.
- Each row displays a `"Dividend"` label above the percentage figure.
- Reward icons per row appear to be three small item icons (quantities
  such as 1,000 / 100 / 2,000 visible for rank 1, 500 / 40 / 1,800 for
  rank 2, 300 / 30 / 1,500 for rank 3, 250 / 20 / 1,000 for rank 4).
  *(TODO: confirm exactly what item types these icons represent — likely
  trade goods or investment-reward currencies.)*
- The current player's own row (Tumbler) is pinned at the bottom of the
  list, separated from the ranked rows, with 0 P and no dividend or
  reward assigned.
- *(TODO: confirm whether individual leaderboard rows are tappable for
  detail drill-down or are display-only.)*

### Right panel — Investment controls

Located along the right edge of the screen.  Contains:

#### Investment Point Goal tracker

- `panel_title`: **"Invest"** (top of right panel)
- `info_label`: **"Investment Point Goal 0 / 500"** — shows current
  progress toward the weekly investment point goal.  The progress bar
  below it appears empty in this frame.

#### Per-category invest rows

Three `right_panel_row` entries, each showing:
- Category name + level
- A filled progress bar (green) indicating points already invested
- An **"Invest"** `action_button` (gold / active)

Observed rows:

| Category | Level | Points shown | Button state |
|----------|-------|-------------|--------------|
| Industry | LV 10 | 8,689,999 | Invest (active) |
| Commerce | LV 10 | 8,683,934 | Invest (active) |
| Military | LV 10 | 8,689,999 | Invest (active) |

- All three progress bars are fully filled (green), suggesting these
  categories are already at or near maximum investment for the cycle.
  *(TODO: confirm whether the numeric value is total invested this cycle,
  cumulative lifetime investment, or category capacity.)*
- The **"Invest"** buttons appear gold/active for all three rows; tapping
  one presumably opens an investment-amount input or increments investment.

#### Calculation countdown

- At the bottom of the right panel: `status_message` **"Until
  Calculations 3d left"** — a greyed / non-tappable label indicating
  time remaining until weekly investment results are calculated.

### Bottom status message

A `status_message` spanning the full bottom of the screen reads:

> "Investments cannot be made after 23:30 on Sunday due to the
> calculation of results, and rewards can be obtained in [Company
> Overview] > [Investment Progress] after 00:10 on Monday."

This is a read-only informational notice; it is not tappable.

### Bottom-left

- **Language Effect** `button` — bottom-left corner, consistent with
  other building sub-menus.

## Element roles

- `mode_tab` — Private / Nation / Guild / Guild Only tab buttons at the top
- `info_label` — column headers (Investment Point, Tax Perks, Reward);
  Investment Point Goal progress label; "Until Calculations 3d left"
- `tappable_row` — ranked investor rows (1–N) and the player's own pinned row
- `status_badge` — "Unavailable" on Manage Market Event sub-menu item;
  appellation / streak badge on rank-1 entry
- `appellation` — small badge on rank-1 investor name *(TODO: confirm role)*
- `right_panel_row` — Industry LV 10 / Commerce LV 10 / Military LV 10
  invest rows
- `action_button` — gold "Invest" buttons on each right-panel category row
- `progress_bar` — green fill bars under Investment Point Goal and per each
  category row
- `status_message` — bottom-screen investment timing notice; "Until
  Calculations 3d left" countdown
- `currency_label` — top-right numeric counters (ducats, gems)
- `chrome_icon` — top-right cluster (settings / mail / home icons)
- `text` — dividend percentage labels (5%, 4%), investment point values
  (e.g. "10,124,319 P"), reward quantity labels, player/company names
- `icon` — reward item icons within leaderboard rows
- `button` — Language Effect (bottom-left)
- `submenu_item` — Tax, Manage Market Event (left strip; documented in
  parent `_building.md`)

## Hints

- **"Unavailable" badge on Manage Market Event** means that sub-menu
  cannot be entered in the current game state; the bot should not attempt
  to navigate to it.
- **Player's own row is pinned at the bottom** (rank `"-"`, name
  "Tumbler" in this frame) regardless of actual rank position.  It is
  always the last visible row.
- **"Until Calculations 3d left"** is a read-only countdown, not a
  button.  Do not attempt to tap it.
- **Bottom status message is read-only** and not tappable — it is a
  timing rule about the weekly investment window.
- **All three category "Invest" buttons may be gold even when the
  Investment Point Goal shows 0/500** — the goal tracker and the per-
  category invest actions appear to be independent.  *(TODO: clarify
  the relationship between Investment Point Goal progress and per-
  category invest actions.)*
- **Reward icon quantities in leaderboard rows** (e.g. 1,000 / 100 /
  2,000) likely correspond to three distinct reward item types, but the
  item types are identified only by small icons.  *(TODO: identify the
  three reward item types by capturing a tooltip or zoomed frame.)*
- **The tab bar scope (Private / Nation / Guild / Guild Only)** changes
  whose investments are shown in the leaderboard but the right-panel
  invest controls appear constant regardless of tab.  *(TODO: confirm
  whether right-panel controls change per tab.)*

## Source frames

- `data/sessions/2026-04-15_16-05-29/frames/0101_1619399899.png` —
  Invest sub-menu, Private tab active, leaderboard showing 5 named
  entries plus player's own pinned row (0 P), all three category Invest
  buttons active, Investment Point Goal at 0/500, countdown 3d left.
- *(TODO: capture a frame with Nation / Guild / Guild Only tabs active
  to confirm whether leaderboard content changes and whether the right
  panel differs.)*
- *(TODO: capture a frame mid-cycle (non-zero Investment Point Goal
  progress) to confirm progress-bar behaviour.)*
- *(TODO: capture a frame showing the result of tapping an "Invest"
  button to document the input widget or confirmation dialog.)*
