"""Buying, as one action per tick.

The steps `buy_to_goal` takes, with the waiting handed back to the dispatcher. Nothing here
is new work: `buyable_now` chooses, `_find_material_tile` locates, `_find_purchase_commit`
finds the button, `refresh_market` restocks, `material_states` decides when it is done. They
are called from a handler instead of from a `for attempt in range(max_rounds)`.

THE CART'S STATE IS READ, NEVER REMEMBERED. A price appears beside `Purchase` only when
something is staged — measured on `trace_barter_cmd_2026-09-05T21-02-24`:

    frame 64 (empty cart)   ['Purchase']
    frame 68 (110 staged)   ['63,360', 'Purchase']

So `_cost_of(commit) > 0` is the whole test, and `MarketState` holds no `staged` flag.

AND A SECOND TAP ON A STAGED TILE UN-STAGES IT. That is why the test above matters more than
it looks: "no commit button" is ambiguous — a swallowed tap, or a detector that missed one —
and re-staging on it destroys a correct cart. `purchase_goods` learned this in August and
answered by refusing to retry at all, which is safe but turns a swallowed tap into a failed
leg. A COST OF ZERO is not ambiguous: the cart is empty, so the tap never landed, and staging
again is right. That is the improvement a tick buys over the refusal.

WHAT THE LEDGER IS TOLD, AND WHEN. Units bought are not in the result card — it reports
money — so they come from the SHELF DROP: the tile's stock before the purchase, minus after.
`state.last_signature` carries the before-reading across the ticks in between, which is the
same mechanism the sell page uses to spot a swallowed tap.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from loguru import logger

# One re-stage, then report — `village._MAX_SELECT_ATTEMPTS`, and the remedy in
# `memory/the-sell-tab-is-where-the-hold-lives`.
_MAX_STAGE_ATTEMPTS = 1
# How many ticks to keep looking for a restock control that is not on screen. The
# usual cause is the live screen having drifted from the tick's, which one
# re-perceive fixes; a shelf with no control at all is a fact, and three looks is
# enough to tell them apart without grinding.
_MAX_RESTOCK_LOOKS = 3


def shelf_signature(goods: Mapping) -> tuple:
    """The shelf, as a comparable reading: what is on sale and how much of it.

    The before/after pair is how many units a purchase actually took — the result card
    reports money, not units.
    """
    out = []
    for name, g in sorted((goods or {}).items()):
        qty = getattr(g, "available_qty", None)
        out.append((str(name), -1 if qty is None else int(qty)))
    return tuple(out)


def credit_the_shelf_drop(state, before: tuple, after: Mapping) -> list:
    """Tell the ledger what the shelf lost. Returns what was credited.

    THE SHELF SAYS WHAT WAS BOUGHT when the owned count cannot be read — one good in the
    round makes the drop that good's purchase, and it depends on none of the panels that
    fail. Live 2026-08-26: `'Pig' owned was unreadable; its tile reads 684` then `reads 0`.
    """
    credited = []
    now = shelf_signature(after)
    was = dict(before or ())
    for name, qty_now in now:
        qty_before = was.get(name)
        if qty_before is None or qty_before < 0 or qty_now < 0:
            continue                              # unread either side — no claim to make
        drop = qty_before - qty_now
        if drop > 0 and state.ledger is not None:
            state.ledger.bought(name, drop)
            credited.append((name, drop))
    return credited


def _tap_the_restock_control(frame, tap_fn, good: str) -> dict:
    """Press the restock control on THIS tick's frame. One action, no waiting.

    Never a currency that is not a confirmed BLUE gem: red gems are real money, and looking
    again cannot change a price. `acted` tells the caller whether anything was spent, which is
    what separates "the control was not on screen this look" from "this shelf will not
    restock" — see the caller.
    """
    try:
        from vision.region_detectors.market_restock import find_restock_button
        btn = find_restock_button(frame)
    except Exception as exc:                  # noqa: BLE001 — blind, not broken
        logger.debug(f"[market] could not look for the restock control: {exc}")
        return {"ok": False, "acted": False, "reason": "the restock control could not be read"}
    if btn is None:
        return {"ok": False, "acted": False,
                "reason": "no restock control (market fresh or not on Purchase grid)"}
    if getattr(btn, "currency", None) != "blue_gem":
        return {"ok": False, "acted": True, "refused": True,
                "reason": f"restock cost is {getattr(btn, 'currency', None)} "
                          "(not a confirmed blue gem) — refused"}
    logger.info(f"[market] {good!r} is sold out and still wanted here — tapping the restock "
                f"@ ({btn.cx},{btn.cy}) (timer read {btn.timer})")
    tap_fn(btn.cx, btn.cy)
    return {"ok": True, "acted": True, "currency": "blue_gem"}


def _safe_total(frame) -> Optional[int]:
    """The hold's total, or None. Never raises — a missing reading is one fewer source."""
    try:
        from actions.buy_materials import _safe_cargo_total
        return _safe_cargo_total(frame)
    except Exception as exc:                  # noqa: BLE001
        logger.debug(f"[market] cargo total unreadable: {exc}")
        return None


def _credit_the_cargo_rise(state, orders: Mapping, goods: Mapping, frame,
                           cargo_before: Optional[int]) -> list:
    """What the HOLD gained across the purchase, when the shelf could not say.

    A SOLD-OUT SHELF READS AS UNREADABLE, NOT ZERO — rightly, since the reader genuinely
    failed — so the one purchase that empties a shelf teaches the ledger nothing. Live
    2026-09-07 that was every purchase: Pig sat at its seeded 1,505 for twenty minutes and a
    dozen buys across two ports, read short against 1,755 the whole time, and filled the hold
    to 4,952/4,952 with Pig while the Raisin that gates the barter had nowhere to go.

    ONLY WHEN ONE GOOD CAN BE ATTRIBUTED. The cargo total is an aggregate, so it names no
    good; with a single order material stocked here every unit it gained is that material's,
    and with two it says nothing about either. Same rule `buy_to_goal` states for its own
    aggregate: "ONE GOOD IN THE ORDER MAKES AN AGGREGATE PER-GOOD".

    Supplies do not move during a buy, so the difference is the purchase.
    """
    if cargo_before is None or state.ledger is None:
        return []
    from actions.buy_materials import tile_in_stock
    here = [m for m in (orders or {})
            if (goods or {}).get(str(m).lower()) is not None]
    if len(here) != 1:
        return []
    now = _safe_total(frame)
    if now is None:
        return []
    gained = now - int(cargo_before)
    if gained <= 0:
        return []
    material = here[0]
    state.ledger.bought(material, gained)
    logger.info(f"[market] the hold rose {cargo_before:,} -> {now:,} across that purchase — "
                f"crediting {gained} {material!r} (the shelf could not be read)")
    return [(material, gained)]


def _stocked_but_unmoved(state, orders: Mapping, goods: Mapping, *,
                         before: Optional[tuple] = None) -> Optional[str]:
    """A wanted good that is ACTIVE and READABLE, whose shelf did not move across a purchase.

    THE ROOM IS THE PROBLEM, NOT THE SHELF (user, 2026-09-04: *"blue gem should only be used
    when the tile is greyed out and stock is 0; on 259 it should not use blue gem to refresh
    as it is still available"*). Buying nothing from a shelf the reader can SEE is stocked
    means the hold could not take it, and no amount of restocking fixes that.

    Live 2026-09-04 at Madeira, frame 259: Raisin 217 on the tile, fully active, 105 free
    slots, and the hold full of the Pig surplus. The buy raised *"The Cargo Hold's Trade Goods
    slot will be exceeded by 52 slots"*; the loop read the 0 as a possible sold-out, spent a
    gem at 205 and rising, then discovered the hold was full one round later anyway.

    UNREAD IS NOT UNMOVED. A shelf missing from either reading yields no claim — the same
    rule `credit_the_shelf_drop` follows, and the reason this cannot fire on a bad parse.
    """
    from actions.buy_materials import tile_in_stock
    was = dict(before if before is not None else (state.last_signature or ()))
    now = dict(shelf_signature(goods))
    for material in orders or {}:
        key = str(material).lower()
        good = (goods or {}).get(key)
        if good is None or not tile_in_stock(good):
            continue                              # sold out — that IS a stock problem
        before, after = was.get(key), now.get(key)
        if before is None or after is None or before < 0 or after < 0:
            continue                              # unread on one side — no claim to make
        if before == after:
            return str(material)
    return None


def on_purchase_page(state, goal, port, *, frame, capture_fn, tap_fn, omni_fn, set_bulk_fn=None) -> dict:
    """One action on the purchase grid.

    Exactly one of: staged / committed / refreshed / waited / finished / blocked.
    """
    from actions.buy_materials import (_cost_of, _find_material_tile, _find_purchase_commit,
                                       buyable_now, material_states, tile_in_stock)
    from vision.market_reader import read_market_page_omni

    els = omni_fn(frame)
    # KEYED BY LOWERED NAME, the shape `buyable_now` and `tile_in_stock` expect — the same
    # `_read` the old loop used, so the readers cannot disagree about what a grid is.
    # ONLY THE GOODS THIS VISIT IS ABOUT get an LLM call when OCR cannot price them. The
    # hold's own goods are included because a purchase decision compares against what we
    # already carry. See `_apply_claude_fallback`.
    wanted = {str(g).strip().lower() for g in (getattr(goal, "orders", None) or ())}
    rows = read_market_page_omni(frame, tab="purchase", port=port, elements=els,
                                 prices_for=wanted or None) or []
    goods = {(str(getattr(g, "name", "")) or "").lower(): g for g in rows}
    if not goods:
        # AN EMPTY READ IS A PERCEIVE FAILURE, not an empty shop — a blocker over the grid,
        # or the greeting page. The dispatcher clears what is in the way; this hands back.
        return {"do": "waited", "why": "the Purchase grid did not read"}

    commit = _find_purchase_commit(frame, els)
    staged_cost = _cost_of(commit) if commit is not None else 0

    if staged_cost > 0:
        # THE CART IS LOADED — the price says so. Commit, and remember the shelf as it stands
        # so the next tick can tell what the purchase took.
        state.landed("stage")
        logger.info(f"[market] the cart holds {staged_cost:,} ducats' worth — purchasing")
        state.did("tapped Purchase", shelf_signature(goods))
        # AND IN ITS OWN SLOT, which survives the confirm and result cards this tap raises.
        # `last_intent` describes the previous TICK, and those cards are ticks of their own.
        #
        # TWO READINGS, because the first one fails exactly when it matters. The shelf drop
        # says what was bought — until the purchase EMPTIES the shelf, when the tile reads
        # `available_qty=None` and no drop can be computed. The CARGO TOTAL is on the same
        # screen, it rises with every purchase, and it does not care whether the shelf is
        # legible (user, 2026-09-07: "it should be reading the pigs now in the cargo, it
        # increases after every purchase").
        state.awaiting_credit = (shelf_signature(goods), _safe_total(frame))
        tap_fn(commit.cx, commit.cy)
        return {"do": "committed", "cost": staged_cost}

    orders = dict(getattr(goal, "orders", {}) or {})
    _note_seasons(port, orders, goods)

    # A PURCHASE JUST COMPLETED? The shelf will have dropped. Credit it before deciding
    # anything else, or the goal test runs on a ledger that has not heard about the last buy.
    if state.awaiting_credit:
        before, cargo_before = state.awaiting_credit
        credited = credit_the_shelf_drop(state, before, goods)
        if not credited:
            credited = _credit_the_cargo_rise(state, orders, goods, frame, cargo_before)
        stuck = None if credited else _stocked_but_unmoved(state, orders, goods,
                                                           before=before)
        state.awaiting_credit = None
        state.did(None)
        if not credited:
            # NEITHER READING LANDED, so what the ledger holds is now KNOWN to be stale
            # is now KNOWN to be stale — and a belief known to be stale is worse than none.
            #
            # The drop is uncomputable exactly when it matters most: a bought-out shelf reads
            # `available_qty=None`, which `shelf_signature` records as -1 and
            # `credit_the_shelf_drop` rightly refuses to treat as zero ("unread is not zero").
            # So the one purchase that empties a shelf teaches nothing.
            #
            # Live 2026-09-07: Pig was seeded at 1,505 and stayed 1,505 for twenty minutes and
            # a dozen purchases across two ports — every buy emptied the shelf, every credit
            # was skipped. It read short against its 1,755 target the whole time, kept buying,
            # and filled the hold to 4,952/4,952 with Pig, leaving no room for the Raisin that
            # actually gates the barter.
            #
            # Dropping the ledger makes the next tick re-read the hold from the sell grid,
            # which is authoritative. It costs a tab switch, and only on a purchase whose
            # shelf could not be read — and the seed hands the Purchase page back now, so it
            # no longer breaks the restock that follows.
            logger.info("[market] the shelf could not be read across that purchase — "
                        "re-reading the hold rather than keeping a count we know is stale")
            state.ledger = None
        if credited:
            logger.info(f"[market] the shelf dropped {credited} — credited to the ledger")
        elif stuck:
            logger.info(f"[market] bought 0 while {stuck!r} is still in stock — the shelf is "
                        "not the problem, the room is; stopping rather than spending a gem "
                        "that cannot help")
            return {"do": "finished", "why": f"bought 0 while {stuck!r} is still in stock — "
                                             "the hold has no room", "cargo_full": True}

    if state.ledger is not None:
        states = material_states(state.ledger, dict(getattr(goal, "orders", {}) or {}))
        if states and all(s["state"] == "met" for s in states.values()):
            return {"do": "finished", "why": "goal met", "materials": states}

    buyable = buyable_now(orders, goods, state.ledger, True)
    if buyable:
        # PUT IN BULK ON, BEFORE A TILE IS TAPPED (user, 2026-09-08: "It is faster to check
        # Put In Bulk"). With it on a tile loads the whole stack in one tap; with it off the
        # same tap opens the per-item Trade Goods Info card, which is Max-then-Load — three
        # taps and two more ticks for the same result.
        #
        # This path never checked. `sell_down_to` turns the box OFF to trim and restores it,
        # and the buy simply assumed it was on — so whenever anything left it off the buy met
        # a card it had no answer for. Live 2026-09-08 at Jakarta the trim ran, found nothing
        # to trim and so never touched the box, and the first Ebony tap opened the card:
        # "gather:Jakarta: a confirmation dialog nobody will answer".
        #
        # CHECK BEFORE ACTING (Guiding Principle #6). The card is still answered when it does
        # appear — the box may be off for reasons of its own — but it need not appear.
        if set_bulk_fn is None:
            from actions.market_actions import _ensure_bulk_mode as set_bulk_fn
        try:
            from actions.market_actions import _is_bulk_mode_on
            if not _is_bulk_mode_on(frame):
                set_bulk_fn(True, frame)
                state.did("turned Put In Bulk on")
                return {"do": "waited", "why": "Put In Bulk was off — a tile tap would open "
                                               "the goods card instead of loading the shelf"}
        except Exception as exc:              # noqa: BLE001 — the card path still answers it
            logger.debug(f"[market] could not read Put In Bulk: {exc}")
        if state.last_intent == "staged" and state.last_signature == shelf_signature(goods):
            # THE CART IS EMPTY AND THE SHELF HAS NOT MOVED: the tap never landed. Unlike a
            # missing commit button, a zero cost is unambiguous, so staging again is safe.
            if state.repeated("stage", _MAX_STAGE_ATTEMPTS):
                return {"do": "blocked",
                        "why": (f"staged {buyable} {_MAX_STAGE_ATTEMPTS + 1}x and the cart "
                                "stayed empty — the tile is not taking taps")}
            logger.warning("[market] the cart is still empty after staging — that tap did "
                           "not register; staging again")
        tapped = []
        for material in buyable:
            tile = _find_material_tile(els, material)
            if tile is None:
                continue
            logger.info(f"[{port}] load {material} — tap tile @ {tile}")
            tap_fn(*tile)
            tapped.append(material)
        if not tapped:
            return {"do": "waited", "why": "nothing on the grid to tap this tick"}
        state.did("staged", shelf_signature(goods))
        return {"do": "staged", "goods": tapped}

    # NOTHING BUYABLE HERE. Either everything this port sells is met, or its shelves are
    # empty — and only the second is worth a gem.
    empty = [m for m in orders
             if (goods.get(m.lower()) is not None and not tile_in_stock(goods[m.lower()]))]
    if empty and _worth_a_gem(state, orders, goods):
        # A SCARCE SEASON IS NOT WORTH GRINDING. The refresh still works at a low-season port
        # — it just returns a quarter of the goods for the same gem, so the answer is another
        # port, not another gem. Live 2026-09-06: Faro returned ~457 Pig per refresh and
        # Madeira ~110 Raisin; ten refreshes and 45 minutes still left Raisin short, capping
        # the barter at 6 rounds. Report it and let the mission choose, rather than paying
        # eleven gems at a time to find out.
        if _season_here(port, empty[0]) == "low":
            # BUY WHAT IS HERE AND LEAVE (user, 2026-09-07: "if the stock is low, instead of
            # refreshing, just buy what is at the market and leave the market and report low
            # stock"). Everything buyable was staged above; only an empty shelf reaches here,
            # so there is nothing left to take and no reason to pay for a quarter-rate refill.
            #
            # This used to allow two gems first, to be sure the season reading was not a
            # one-frame fluke. Live 2026-09-07 at Madeira the ribbon read `low` on every one
            # of six looks across nine minutes — the reading is not the doubtful part. The two
            # gems bought ~110 Raisin apiece and delayed the report that matters.
            return {"do": "finished", "season": "low", "good": empty[0],
                    "why": (f"{empty[0]!r} is scarce here this season — buying what is on the "
                            "shelf and reporting rather than paying for a quarter-rate refill")}
        # ONE TAP, THEN HAND BACK. This used to call `refresh_market`, which captured a fresh
        # frame, tapped the control, captured again to find the Replenish-Stock OK by OCR,
        # tapped that, captured a third time to verify the tile, and could SLEEP up to 90
        # seconds waiting out a nearly-expired timer — a whole perceive-decide-act flow inside
        # one tick, and the market's own `restock_prompt` context sat unhandled beside it.
        #
        # The tick shape needs none of it. Tap the control; the dispatcher perceives the
        # Replenish card and hands it to `_on_restock_prompt`; the tick after that reads the
        # grid and simply SEES whether the shelf refilled. The verification is the next look.
        #
        # The wait-out case dissolves rather than being ported (live 2026-09-05 at Madeira:
        # the ↻ went in at 00.00:11, the game was already turning the market over, no dialog
        # appeared, and the unconfirmed refresh broke the leg 11 seconds before the shelf
        # refilled for free). There is no "failed to confirm" step here to recover from — the
        # next tick looks, and a shelf that refilled by itself is simply a shelf with stock.
        res = _tap_the_restock_control(frame, tap_fn, empty[0])
        state.did("tapped the restock")
        if not res.get("ok"):
            # A REFUSAL THAT SPENT NOTHING IS ABOUT THIS LOOK, NOT ABOUT THE SHELF.
            #
            # `acted=False` means the restock control was not on screen — which is true
            # whenever the LIVE screen has drifted from the tick's, and the seed's side trip
            # to the Sell tab is exactly that. Ending the leg on it treats one dropped tap as
            # a fact about the world.
            #
            # Live 2026-09-06 at Madeira: ten refreshes had already succeeded in that same
            # leg, so the shelf was plainly refreshable. Then a late re-seed switched tabs,
            # its switch-back tap did not land, and frame 382 shows the Sell panel at the
            # moment of the refusal. The leg finished with Raisin at 1,100 of 1,755 — which
            # capped the barter at 6 rounds instead of 7 and left 729 Pig unused.
            #
            # So: hand back and look again, BOUNDED. Anything that ACTED still finishes —
            # the tap went in and a gem may be spent, or the price was not blue gems, and
            # neither is improved by looking twice.
            if not res.get("acted") and not state.repeated("restock_look", _MAX_RESTOCK_LOOKS):
                logger.warning(f"[market] no restock control on screen for {empty[0]!r} — "
                               "looking again rather than ending the leg")
                return {"do": "waited", "why": "the restock control was not on screen"}
            return {"do": "finished", "why": f"no refresh for {empty[0]!r} — "
                                             f"{res.get('reason', 'not confirmed')}"}
        state.landed("restock_look")
        return {"do": "refreshed", "good": empty[0]}

    return {"do": "finished", "why": "nothing here is still wanted"}


def _note_seasons(port: str, orders: Mapping, goods: Mapping) -> None:
    """Record what the shelves say about the season, for the goods this mission wants.

    Written on SIGHT rather than on failure: by the time a leg has ground through ten
    refreshes the information has already cost what it was worth.
    """
    if not port:
        return
    try:
        from memory.market_kb import note_season
        for material in orders or {}:
            good = (goods or {}).get(str(material).lower())
            if good is None or getattr(good, "sold_out", False):
                continue          # a sold-out tile has no readable season — see `tile_season`
            note_season(port, str(material), getattr(good, "season", None))
    except Exception as exc:                  # noqa: BLE001 — bookkeeping, not the buy
        logger.debug(f"[market] could not record the season: {exc}")


def _season_here(port: str, good: str) -> Optional[str]:
    """What we know about this good's season at this port, or None."""
    try:
        from memory.market_kb import season_of
        return season_of(port, good)
    except Exception as exc:                  # noqa: BLE001
        logger.debug(f"[market] could not read the season: {exc}")
        return None


def _worth_a_gem(state, orders: Mapping, goods: Mapping) -> bool:
    """Is anything still short AND sold here? The guard from `buy_to_goal`, unchanged.

    A refresh is MARKET-WIDE, so the good that justifies it need not be the one whose shelf
    emptied — Barcelona stocks Iron and Matchlock Gun, and with Iron met and Matchlock short
    the restock is exactly right. What it rules out is Faro: Pig met, Raisin short, Raisin
    not sold there at all.
    """
    from actions.buy_materials import material_states
    if state.ledger is None:
        return True
    try:
        for material, s in material_states(state.ledger, dict(orders)).items():
            if s["state"] == "met":
                continue
            if (goods or {}).get(material.lower()) is not None:
                return True
    except Exception as exc:
        logger.debug(f"[market] could not weigh the refresh: {exc}")
        return True                               # cannot rule it out — behave as before
    return False


def on_goods_info(state, goal, *, frame, tap_fn, omni_fn) -> dict:
    """The Trade Goods Info card, during a BUY. Max, then Load — one per tick.

    THIS CARD IS WHAT A TILE TAP GIVES YOU WITH `Put In Bulk` OFF. With it on, a tile
    bulk-loads the whole stack and no card appears, which is why the buy never met one on the
    dispatcher path — and why it had no answer when it did.

    The handling is not new: `_sell_one_good` waits for this card and taps `Max`, and
    `_buy_load_one_good` types a quantity and taps `Load`. Both live on the old `buy_goods` /
    `sell_goods` flows, which the mission stopped using (user, 2026-09-08: "This dialog we
    should have code to handle it, it was supported for sure"). This is the same two steps on
    the tick path.

    Live 2026-09-08 at Jakarta (frame 28 of trace_barter_cmd_2026-09-08T10-31-17): the card
    came up for Ebony with `Cancel` and `Load` and a `1/158` spinner, `Put In Bulk` unchecked
    behind it. Nothing owned it, `game_rules` correctly refused to press a lone `Cancel`, and
    the leg died with "a confirmation dialog nobody will answer".

    MAX, NOT A TYPED FIGURE. The buy already takes whole shelves — `buy_to_goal` "buys whole
    shelves, so the hold arrives over-stocked", which is what the trims either side are for —
    so Max is what bulk-loading would have done, without a keypad to get wrong.
    """
    from actions.sail_actions import _find_button

    if state.last_intent == "tapped Max on the goods card":
        load = _find_button(frame, "load")
        if load is None:
            return {"do": "blocked", "why": "no Load button on the goods card"}
        tap_fn(*load)
        state.did("tapped Load on the goods card")
        return {"do": "staged", "why": "loading the shelf from the goods card"}

    mx = _find_button(frame, "max")
    if mx is None:
        return {"do": "blocked", "why": "no Max button on the goods card"}
    tap_fn(*mx)
    state.did("tapped Max on the goods card")
    return {"do": "waited", "why": "taking the whole shelf on the goods card"}
