"""Game-knowledge primer for the reasoning layer.

The compact, curated background knowledge the LLM reasons with (the GAME
KNOWLEDGE block of the reasoning context). Kept SHORT on purpose (B8 rule:
bounded clean text) — this is the starting point, meant to be **iterated from the
reasoning traces**: when a trace shows the LLM lacked a fact, add it here.

See docs/reasoning_fallback_layer_design.md (§4 input contract, §12 methodology)
and docs/bot_architecture_layers.md (Company world model, many-to-many skills).
"""

GAME_PRIMER = """\
GAME: Uncharted Waters Origin. The player runs a trading COMPANY.

CURRENCIES (company-wide):
- ducat: trade money (buy/sell goods). Free to spend.
- blue_gem: buys items, ship restore, etc. Free to spend.
- red_gem: REAL MONEY — NEVER spend or confirm a red-gem purchase autonomously.
- guild_coin and others: spend on their own specific things.

FLEET & SHIPS (the unit that sails):
- A fleet has ships. Each ship has a 'life' (durability) that drops while sailing.
- A ship with life < 20 CANNOT sail and blocks the whole fleet. Restore it at the
  Shipyard (or Harbor) using a blueprint or blue gem.
- To depart a port and sail, the fleet needs: full CREW, ships' life OK, and supplies.

CREW (sailors that man the ships):
- Recruit crew at the HARBOR, the INN, or a VILLAGE (Village gives limited numbers;
  Harbor is usually fastest). Use the 'Recruit' / 'Recruit Crew' menu item.
- Crew (sailors) is DIFFERENT from mates: 'Hire' at the Inn hires MATES, not crew.
- On the Recruit panel tap 'Min' for the minimum crew needed to sail — use this
  NORMALLY. 'Max' (full crew) is only needed for BATTLE. And prefer 'Normal Recruit'
  over 'Emergency Recruit' — Emergency costs vastly more ducats for the same crew.

BUILDINGS ↔ FUNCTIONS (a function is offered at several buildings; a building
offers several functions). Approximate map:
- Harbor:   Supply/resupply, Repair, Recruit Crew, depart to sea.
- Inn:      Recruit (crew), Hire (mates), Party, Employee, Manage Mate.
- Shipyard: Build, Repair, Restore ship, Parts, Modify, Dismantle.
- Market:   Purchase goods, Sell goods (the trade grid).
- Bank:     Deposit/Withdraw, Savings, Insurance.
- Village:  Explore, Loot, Gifting, Barter, Recruit Crew (limited).

TRANSACTIONS (recruit / buy / sell — usually a right-side panel):
- MARKET BUY/SELL — LOAD ALL, THEN COMMIT ONCE (important for speed): buying is
  TWO phases. (1) LOAD: for EACH good you want, tap its grid tile → a 'Trade Goods
  Info' dialog opens (price + a quantity control with Min/Max) → tap 'Max' (or set
  a quantity) → tap 'Load'. This only ADDS the good to the cart; it does NOT buy.
  Repeat for every good you want. (2) COMMIT: tap the yellow 'Purchase' button
  ONCE — it buys the WHOLE cart in a single transaction (→ Confirm Purchase → OK →
  maybe a load-ratio warning → negotiation). Do NOT tap Purchase after each good —
  that buys one item at a time and is very slow. Load everything first, then
  Purchase once. (Sell is the mirror: 'Load All' → 'Sell' once.)
- The panel has: an AMOUNT control (a slider / '953/2,275' with Min/Max), sometimes
  TYPE-option rows (e.g. 'Normal Recruit' vs 'Emergency Recruit', or a tax/price
  option) that only pick a mode — tapping them does NOT complete the transaction,
  and a final COMMIT button labelled with the BARE action verb ('Recruit', 'Buy',
  'Purchase', 'Confirm'), usually at the bottom of the right panel.
- The final commit button is the YELLOW one (cost on the left, verb on the right).
  It is surfaced in the element list tagged `[COMMIT] <verb> — cost <amount>`
  (e.g. `[COMMIT] Recruit — cost 205,848`). To COMPLETE a transaction, tap that
  `[COMMIT]` element (arg = its verb). Option rows like 'Normal Recruit' /
  'Emergency Recruit' only pick a mode — they do NOT complete it.
- PRICE NEGOTIATION: after you tap OK on a buy/sell Confirm, an 'Attempt
  Negotiation' dialog often appears — a mate offers to HAGGLE THE PRICE DOWN (e.g.
  "we should be able to buy this for 21% less. Should I negotiate?") and shows a
  'Negotiation Success Rate' and 'Remaining negotiation attempts'. This is part of
  BUYING/SELLING — it is NOT a lottery / gacha / chance panel (ignore the dice
  icons). Buttons: 'Use 1 chance' (spend ONE attempt to push the discount higher),
  'Use all remaining chances', or 'No' (STOP haggling and take the discount you
  already have). It is REPEATED: each successful attempt improves the discount and
  offers another round, using one of the 'Remaining negotiation attempts'.
  CRITICAL RISK: if any attempt FAILS, you LOSE ALL the discount accumulated so far
  (it resets to full price) — not just that one attempt. So this is a real gamble,
  and the more discount you have built up, the MORE you stand to lose by continuing.
  It is a risk/reward STOPPING decision — you do NOT have to exhaust all attempts:
    • Only keep tapping 'Use 1 chance' while the discount is still SMALL (little to
      lose) AND the success rate is high.
    • Tap 'No' to LOCK IN the current discount once it's already worth protecting
      (e.g. ~20%+), or the success rate isn't clearly in your favour — take the sure
      discount rather than gamble it all away.
  After 'No' (or exhausting the chances), confirm / dismiss any result dialog. Do
  NOT tap Purchase again or navigate away — the transaction is not complete until
  this dialog is cleared.

CARGO & LOAD RATIO (when buying trade goods):
- The fleet's cargo capacity is split by a LOAD RATIO between SUPPLIES and TRADE
  GOODS. Loading trade goods past the trade-reserved share pops a warning dialog.
- On that warning, reason about it: you may PROCEED (tap OK/Confirm) if supplies
  are still sufficient for the voyage, OR reduce the amount of trade goods. Use the
  supply days / cargo numbers on screen to judge.
- The 'Apply Load Ratio' checkbox (bottom of the market page), when checked, makes
  loading AUTO-RESPECT the ratio — goods stop loading at the reserved capacity and
  the warning never appears. Checking it up front avoids the popup entirely.

NAVIGATION:
- On the port overworld, tap a building's icon/label to enter it.
- Back / Home exits a building back to the port overworld.
- Prefer doing a function at the building you are ALREADY in, if it offers it.

SAFETY: never tap a Confirm / Buy Now / Pay that would spend red gems; if that's
the only option, abort and let a human decide.
"""


def primer_for(context: str = "") -> str:
    """Return the primer (optionally focused later per context). For now, the
    full compact primer — small enough to always include."""
    return GAME_PRIMER
