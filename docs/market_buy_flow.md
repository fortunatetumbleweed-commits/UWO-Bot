# Market Buy Flow (London-style port market)

Observed end-to-end 2026-08-07/08 (session on `trading_revisit`). This is the
canonical BUY sequence + the per-dialog policy the bot must follow. The core
principle: **load all desired goods into the cart, then COMMIT once** — never buy
one good at a time (extremely slow).

## The two phases

**Phase 1 — LOAD (build the cart).** For EACH good you want:
1. Tap the good's grid tile → the **Trade Goods Info** dialog opens (price chart,
   Purchase Cost / Tax / Discount, a quantity control `[- N +]` with **Min/Max**,
   Final Purchase Price, `Cancel / Load`).
2. Tap **Max** (or set a quantity) → tap **Load**. This ADDS the good to the cart;
   it does **not** buy anything yet. The right-panel `Cargo X/4108` grows.
3. Repeat for every good you want in this trip.

**Phase 2 — COMMIT (buy the whole cart once).** Tap the yellow **Purchase**
button **once**. It buys the ENTIRE cart in a single transaction, then walks
through the confirm / warning / negotiation dialogs below.

> ⚠️ Do NOT tap Purchase after each good. Purchase commits the whole cart, so
> load→Purchase→repeat buys one item at a time — the slow, wrong pattern the
> LLM fell into.

## The dialogs after Purchase (in order)

| Screen | What it is | Policy |
|---|---|---|
| **Confirm Purchase** | table of loaded goods (Cost/Tax/Discount/Price) + Total, `Cancel / OK` | tap **OK** |
| **Load-ratio warning** *(only if the cart overflows the trade-goods slot)* | *"The Cargo Hold's Trade Goods slot will be exceeded by N slots. Purchase the Trade Goods?"* + *"Do not ask until next startup"* checkbox, `Cancel / OK` | **OK** if supplies are still enough for the voyage (short trip); else **Cancel** + load less. Ticking **Apply Load Ratio** on the grid up front avoids it entirely (loading then auto-respects the ratio). |
| **Attempt Negotiation** | a mate offers X% off; **Negotiation Success Rate**; **Remaining attempts**; `No / Use 1 chance / Use all remaining chances` | **Use 1 chance** while the discount is still small AND success rate high; **No** to LOCK IN a good discount (~20%+). ⚠️ **A failed attempt wipes ALL accumulated discount** (resets to full price) — the more you've built up, the more you risk. NOT a lottery. |
| purchase finalizes | ducats spent, goods in cargo | done |

## Two branch points to disambiguate

- **Loaded a lot (overflow the trade-goods slot ~>4000/4108)** → the **load-ratio
  warning** fires *before* negotiation. This is the "unexpected" dialog that broke
  the scripted flow.
- **Loaded a modest amount (under the slot)** → straight to **Confirm →
  Negotiation**, no warning.

## Related config

- **Set Load Ratio** panel (tabs Custom/Adventure/Trade/Combat): splits the
  `Max Load Capacity` (e.g. 4,108) into Water 8% / Food 8% / Trade Goods 84%
  (3,450) / Material / Ammo. The load-ratio warning is about exceeding the Trade
  Goods share.

## Perception prerequisites (open)

The bot must reliably tell these six screens apart (grid / info-dialog / confirm /
load-ratio / negotiation / set-load-ratio). Two known mix-ups to fix: negotiation
read as a "lottery/chance panel", and confirm read as load-ratio. Also read the
negotiation **discount %** vs **success-rate %** as distinct labeled fields.

See memory: `project_price_negotiation_dialog`, `project_cargo_load_ratio`,
`project_yellow_commit_button_style`, and CLAUDE.md anti-cheat tap discipline
(never hand-drive the market with fast/fixed-cadence taps —
`feedback_manual_playthrough_triggers_anticheat`).
