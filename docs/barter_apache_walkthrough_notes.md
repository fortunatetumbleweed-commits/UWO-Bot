# Apache barter walkthrough (human-played, 2026-08-14) — mechanics extraction

Source: `data/sessions/barter_apache_walkthrough_2026-08-14T12-48-30/` (25 frames,
human-driven; no action log — reconstructed from the screens, summarized 2026-08-20).
This session demonstrates several mechanics the bot has NOT yet executed live:
**negotiation, gifting-to-tier, the exchange quantity stepper, amity-locked goods,
the OVERFLOW DISCARD flow, and remote sell-port scouting.**  Cross-refs:
`docs/barter_command_flow.md`, `docs/village_check_apache_walkthrough.md`.

## Frame-by-frame

| Frames | Screen | What it shows |
|---|---|---|
| 0 | Village (at Apache) | Amity **Neutral 60,000/100,000**; Amity Effect: Discovery 8%, Recruit Max 30, **Increase Barter Count by 2**; Village Status Calm, Stock Change +1.9% |
| 1 | Barter panel | Refresh countdown 05:09:15; 4 goods tiles — one **LOCKED with tooltip "Friendly or more Village Amity"**; Trade Count slots empty |
| 2 | Camas selected | Trade Quantity **709(+109)**, Amity Change +3,153; **quantity stepper `200/200`** (−/+); materials **862/170 Avocado, 881/170 Cassava** (X/Y = have/need at the CURRENT stepper value) |
| 3–4 | **Trade Negotiation dialog** | Quantity vs Price negotiation; success ~8%/8.2%; **costs amity per attempt** (387 single / 9,116 “Bulk Negotiate 8”); attempts show ✓/✗ marks; failures consumed amity (+3,153 → +2,766 pending) |
| 5 | Pulque selected | Stepper at `1/200` (min unit): obtain 3 ← 1 Coral + 1 Silver; materials 0/1 red → **Exchange greyed** |
| 6 | **Barter Calculations** (confirm) | Negotiation Result (Success 4 / Failure 1); amity math: Exchange +3,153 − negotiation −387 = +2,766; **Amity Grade Change Neutral → Favorable**; Final 62,766 |
| 7 | Post-exchange | Ratio **improved immediately at the new tier: 709(+109) → 744(+144)**; materials dropped EXACTLY by need (862→692, 881→711); one Trade Count slot filled with the good icon + qty |
| 8 | Village at Favorable | Effects now 10% / Max 50; red **"Cannot Exchange"** toast on the Barter menu (slots/materials exhausted state exists) |
| 9–12 | **Gifting** | The **amity tier ladder**: Neutral (8%/30/+2) → Favorable (10%/50/+2) → Trusting (12%/75/**+3**) → Friendly (15%/100/**+3**); options: Gift 1 time / Gift up to max / **Gift up to max Amity (uses Friendship Tokens)**; crew confirm dialog; ends **Friendly 100,000/100,000** |
| 13 | Barter at Friendly | 4 Camas slots used; **Wampum is now available — it was the tile LOCKED at frame 1, and the gifting at 9–12 is what unlocked it** (amity gate lifted by reaching Friendly); it shows Amity Change −107, so **some barters REDUCE amity** |
| 14–21 | ⭐ **OVERFLOW DISCARD flow** | Exchange output didn't fit: **"Insufficient Empty Space — … Unreceived trade goods will be discarded"** dialog: pending "Received Trade Goods 143", Cargo 4,108/4,108 with tappable tiles → tapping a tile opens **Discard Goods** (qty stepper + the SAME numeric keypad; partial amounts OK; **even supplies/Food can be discarded** — typed 50/226) → each discard frees space and the pending goods **auto-receive incrementally** (143→131→100→0) → **Receive** closes it |
| 22 | Barter panel after | 5 slots used; Camas materials exhausted (0/1) |
| 23 | ⭐ **Remote sell scouting** | World Map → Port tab → **Veracruz City Info → Trade → Cargo tab**: shows YOUR cargo goods' sell price AT THAT PORT — “Camas 189% 68,191” — sell-port selection without sailing |
| 24 | Edinburgh City Info → **Preference tab** | Category boosts: Textile +50%, **Food +30%** (Camas is Food), Livestock +10%, Spices/Artwork/Fabrics +20% |

## Mechanics the bot must learn from this (none executed live yet)

1. **Exchange quantity stepper (1–200)** — output and material needs SCALE with it; the
   displayed `X/Y` is at the current stepper value.  Min-unit view (stepper 1) exposes the
   base ratio (Pulque: 3 ← 1+1).  This resolves the "stepper 80 / Bulk Exch." open question
   structurally; default appears to be max (200).
2. **Negotiation** — optional, ~8% success, costs amity PER ATTEMPT (visible ✓/✗), bulk
   variant does 7–8 attempts; net amity = exchange gain − negotiation spend.  The current
   default (skip) remains sound; if used, budget amity.
3. **Amity tier ladder with per-tier BARTER-COUNT bonuses** — the daily N/7 itself grows
   with tier (+2/+2/+3/+3 …).  Tier crossings mid-session IMPROVE the ratio immediately
   (709→744 observed).  Gifting (incl. Friendship Tokens) can push to a tier ON PURPOSE
   before bartering — a plannable lever.
4. **Amity-locked goods tiles** ("Friendly or more") and **amity-NEGATIVE barters**
   (Wampum −107/round) — eligibility + strategy inputs.  The lock is purely an amity gate and
   **gifting lifts it on purpose**: Wampum is locked at frame 1 and barterable at frame 13
   because of the gifting at 9–12.  So "locked" is a plannable precondition (gift → unlock →
   barter), not a permanent property of the village.  Deferred — the bot neither reads the
   lock nor gifts today (user 2026-08-20: not a concern for now).
5. ⭐ **Overflow discard flow** — the missing piece for `run_barter_phase`'s jettison hook:
   when output exceeds space, the Insufficient-Empty-Space dialog holds the goods PENDING;
   the bot must tap a cargo tile → Discard Goods → keypad quantity → OK (repeat) → Receive.
   Unreceived goods are LOST if dismissed.  Prefer discarding low-value cargo; supplies can
   be discarded but respect the supply reserve (jettison_planner #21 already encodes this).
6. ⭐ **Remote sell-port scouting** — City Info → Trade → **Cargo** tab prices YOUR goods at
   any port from the world map; **Preference** tab shows category boosts.  This is the
   missing input for choosing the sell port / route destination rationally (sell_port
   selection #22 currently uses distance×preference heuristics; this is ground truth).

## Corrections/confirmations vs current docs
- Confirms `X/Y = have/need` exactly, and per-exchange consumption == displayed need
  (frames 2→7: 862→692 for need 170).
- The amity ladder order is Neutral → **Favorable** → Trusting → Friendly (the Melanesian
  run's "Neutral→Trusting" jump crossed Favorable in one +29,859 step).
- The village screen's "Stock/Negotiation Refresh" countdown (~6h) governs the volatiles.
