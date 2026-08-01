---
name: Sell flow complete — buy flow and sailing next
description: Sell flow working end-to-end; next steps are buy flow capture then world map navigation
type: project
---

Sell flow (`sell_all_cargo`) is fully working as of 2026-04-17:
- Both goods (Dracaena, Magnetic Iron Ore) sold successfully at Diu
- Total received: 993,780 ducats — profit: 588,176
- Negotiation detected and handled correctly
- Flow coordinate scaling (2.4×) fixed — flow.json stores thumbnail coords, bot scales at runtime

**Why:** Root cause of previous failures was flow.json coordinates being in 1000px thumbnail
space while tap() operates on 2400×1080 full screen. Fixed in `_set_flow_scale()`.

**How to apply:** Next session: record `market_buy` flow, then implement `buy_goods()`,
then Milestone 2 world map sailing.

Next steps (in order):
1. `python tools/capture_flow.py market_buy` — with Purchase tab open, capture each screen
2. Wire up `buy_goods()` using the same perception-first pattern as `_sell_one_good()`
3. Milestone 2: world map navigation to connect buy→sail→sell loop across ports
