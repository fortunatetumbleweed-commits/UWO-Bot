# Estate — Insurance sub-menu

The Insurance sub-menu lets the player purchase, review, or cancel a tiered insurance policy for their estate.  Policies are listed vertically in a centre panel; the currently active policy is shown in a detail panel on the right.

## Layout

### Centre panel — Insurance tier list

A scrollable vertical list of insurance tiers occupies the centre of the screen, under a `"Insurance"` panel title.  Each row is a `tappable_row` and shows:

- **Level badge** (e.g. `"LV 1"`, `"LV 2"` … `"LV 5"`) on the left edge of the row — displayed as a diamond-shaped badge; the currently purchased level shows a `"Purchased"` sub-label beneath the level number.
- **Maximum Compensation** — labelled `"Maximum Compensation"` with a gold-coin icon and a numeric value (e.g. `50,000` / `250,000` / `500,000` / `1,250,000` / `3,700,000`).
- **Rate** — displayed to the right of the row as a green percentage label (e.g. `30%` / `40%` / `50%` / `60%` / `65%`).
- **Daily Insurance Fee** — labelled `"Daily Insurance Fee"` with a gold-coin icon and a numeric value (e.g. `0` / `950` / `1,045` / `1,710` / `2,755`).  LV 1 shows `0` as the daily fee.

Observed tiers in order top-to-bottom:

| Level | Max Compensation | Rate | Daily Fee |
|-------|-----------------|------|-----------|
| LV 1  | 50,000          | 30%  | 0         |
| LV 2  | 250,000         | 40%  | 950       |
| LV 3  | 500,000         | 50%  | 1,045     |
| LV 4  | 1,250,000       | 60%  | 1,710     |
| LV 5  | 3,700,000       | 65%  | 2,755     |

A partially visible row labelled `"Recommended"` (green badge) appears below LV 5, suggesting the list is scrollable and at least one additional tier exists below the visible area. *(TODO: capture a scrolled frame to document all tiers including the Recommended one.)*

### Right panel — Insurance Info

A right-side panel titled `"Insurance Info"` shows the details of the **currently active (purchased) policy**:

- **Level badge** — large circular emblem in the centre of the panel, showing the purchased level and `"Purchased"` label (e.g. `"LV 1 Purchased"`).
- **Maximum Compensation** info label — gold-coin icon + numeric value (mirrors the selected tier's value).
- **Rate** label — green percentage to the right of the compensation value.
- **Daily Insurance Fee** label — inside a slightly recessed sub-panel, showing the current daily fee.
- **`"Cancel Insurance"` action button** — gold button at the bottom of the right panel.  Present when a policy is active; allows the player to cancel the current insurance.  *(TODO: confirm whether this button changes to a purchase/upgrade button when a non-purchased tier is selected from the list.)*

### Bottom-left

- **`"Language Effect"` button** — bottom-left corner, shared chrome element.

## Element roles

- `panel_title` — `"Insurance"` (centre panel header), `"Insurance Info"` (right panel header)
- `tappable_row` — each LV 1–LV 5 insurance tier row in the centre list
- `status_badge` — level badge on each row (e.g. `"LV 1"`, `"LV 2"`); the active tier's badge includes a `"Purchased"` sub-label
- `info_label` — `"Maximum Compensation"`, `"Daily Insurance Fee"`, `"Rate"` display fields (read-only; both in the tier list rows and the right panel)
- `currency_label` — numeric compensation and fee values within rows (gold-coin icon prefix)
- `action_button` — `"Cancel Insurance"` (gold, bottom of right panel; only visible when a policy is active)
- `icon` — gold-coin icons prefixing compensation and fee values
- `button` — `"Language Effect"` (bottom-left)
- `text` — static field labels (`"Maximum Compensation"`, `"Daily Insurance Fee"`, `"Rate"`)

## Hints

- **LV 1 Daily Insurance Fee is 0** — the cheapest tier costs nothing to maintain daily; the `"Purchased"` badge on LV 1 in this frame indicates the player has the free base policy active.
- **`"Cancel Insurance"` is the only action button when a policy is already purchased** — the bot should not expect a `"Purchase"` or `"Upgrade"` button while viewing the currently held tier.  *(TODO: confirm what button(s) appear when the player taps a higher-level tier row — likely a purchase/upgrade confirmation button replaces or supplements `"Cancel Insurance"`.)*
- **Rate field labels are not tappable** — the green percentage labels (`30%`, `40%`, etc.) are read-only `info_label` elements displayed inline with the row; tapping the row selects the tier, not the percentage label specifically.
- **`"Recommended"` badge** appears on at least one tier below the visible scroll area.  Its exact level and values are not yet captured.  *(TODO: scroll down and capture the Recommended tier.)*
- **Daily Insurance Fee of 0 for LV 1** may mean the base policy is effectively free to maintain once purchased; the bot should not treat a zero fee as missing data.
- **The right panel reflects the currently purchased policy**, not the currently highlighted/selected row.  *(TODO: verify whether tapping a different tier row updates the right panel preview before purchase confirmation.)*

## Source frames

- `data/sessions/2026-04-15_16-05-29/frames/0035_1610390388.png` — Insurance sub-menu with LV 1 purchased, all five tiers visible, right panel showing LV 1 details and `"Cancel Insurance"` button active.
- *(TODO: capture frame with a higher-level tier selected to confirm right-panel preview behaviour and purchase/upgrade button appearance.)*
- *(TODO: capture a scrolled-down frame to document the `"Recommended"` tier and any additional tiers below LV 5.)*
- *(TODO: capture frame immediately after cancelling insurance to confirm the state of the right panel and action buttons when no policy is held.)*
