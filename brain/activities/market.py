"""The market: one activity, three goals.

`gather`, `sell_surplus` and `sell` are three task nodes today, and each one navigates to the
market before doing its one market thing. They differ only in WHICH goods and HOW MANY —
which is CLAUDE.md's "ONE sell flow, several goals" arrived at from the other direction, and
why `_enter_market_at` had to be patched into four call sites at once on 2026-08-26.

WHAT THIS ACTIVITY DOES NOT DO IS GET TO THE MARKET. Entering a building is a TRANSITION — it
ends one activity and expects another — so it is an intent the dispatcher dispatches, and this
activity begins only once perception already says `building: market` (user, 2026-08-26). It
also never sails, never leaves, and never decides where to go next.

THE GOALS CARRY NO SCREENS. `Hold({"Iron": 242})` says what the hold should contain; nothing
in it mentions a tab, a tile or a bulk checkbox. The quantities are the PLAN'S ESTIMATE and
not a contract — the plan wanted 242 Iron on 2026-08-26 and a bulk tap bought 445, which is
not a failure and nothing treated it as one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from loguru import logger

from brain import owned_state
from brain.dispatcher import ActivityResult, BLOCKED, FINISHED, UNRECOGNISED, WORKING


# ── The goals ────────────────────────────────────────────────────────────────
#
# These are the TASK's vocabulary, kept beside the activity that executes them while the
# pattern is being proven. If a second activity ever needs its own set, they move somewhere
# shared rather than growing a dependency between activities.

@dataclass(frozen=True)
class Hold:
    """Have these goods aboard. Stops when the hold covers them, the shelves are empty, or
    the hold is full — whichever the world reaches first.

    THE WHOLE LIST, NOT ONE GOOD PER VISIT. Only this activity can see what a port actually
    stocks — it may carry one of the three, or all — so splitting the list forces the layer
    above to guess. And a visit that knows the full list will not clear Iron out of the hold
    to make room for Candle; split into three visits, that knowledge is gone and no later
    `sell_surplus` step can recover it.

    The quantities are the PLAN'S ESTIMATE and not a contract — the plan wanted 242 Iron on
    2026-08-26 and a bulk tap bought 445, which is not a failure and nothing treated it as one.
    """
    orders: Mapping[str, int]

    def __str__(self) -> str:
        return "hold " + ", ".join(f"{g} (~{q})" for g, q in self.orders.items())


@dataclass(frozen=True)
class FreeHold:
    """Make room: sell every good not in `keep`, profitable or not."""
    keep: tuple = ()

    def __str__(self) -> str:
        return f"free the hold (keeping {', '.join(self.keep) or 'nothing'})"


@dataclass(frozen=True)
class TrimHold:
    """Cut each named good down to the quantity given; leave everything else alone.

    Distinct from FreeHold, which disposes of whole goods. `buy_to_goal` buys BY THE SHELF,
    so the hold arrives with more than was asked for, and the excess is dead weight the
    barter output then cannot fit. Only goods that were named are trimmed — clearing
    unrelated cargo is a separate, opt-in decision.
    """
    keep_qty: Mapping[str, int]

    def __str__(self) -> str:
        return "trim to " + ", ".join(f"{g} {q}" for g, q in self.keep_qty.items())


@dataclass(frozen=True)
class SellHold:
    """Sell what is profitable, protecting `exclude`."""
    exclude: tuple = ()

    def __str__(self) -> str:
        return f"sell the hold (excluding {', '.join(self.exclude) or 'nothing'})"


# Never sold to make room: the fleet does not sail without them. Same pair the barter
# protect list uses (`actions.sell_goods.barter_materials_exclude`).
_SUPPLIES = ["Water", "Food"]


class MarketActivity:
    """Buy and sell. Nothing else."""

    name = "market"

    # WHERE THIS ACTIVITY CAN WORK, declared once and read by everyone who needs it.
    #
    # Keyed by WHICH BUILDING, because `building` alone is too coarse — the harbour, the
    # market and the shipyard are all `building`, and handing a sell goal to whichever one
    # the bot is standing in is the class of mistake this design removes.
    #
    # It is a class attribute rather than a constant in two modules because it WAS a constant
    # in two modules, for about an hour on 2026-08-26. `brain.intents` compared against
    # ('building', 'sub_menu', 'market') while the registry was keyed ('building:market',
    # ...), so `to_intent` never recognised "already there" and dispatched ENTER_BUILDING on
    # every tick — while the bot stood inside the market. Only `tap_building_entry` refusing
    # to tap a screen that is not the building list kept it from tapping at random in there.
    SERVES = ("building:market", "sub_menu:market", "sub_menu:purchase", "sub_menu:sell")

    # Inside a building: Back works, the ☰ does not exist, and there is no globe. Leaving is
    # the only transition. (The market's own tabs are not transitions — they do not change
    # which world the fleet is in.)
    CAN_START = ("EXIT_BUILDING",)

    # Out of a building is onto the overworld it stands in.
    LEADS_TO = {"EXIT_BUILDING": "port_overworld"}

    # WHICH GOALS IT SERVES, declared for the same reason SERVES is: so nobody restates it.
    # `brain.intents` kept its own tuple of these, and on 2026-08-26 `TrimHold` was added
    # here and not there — so `to_intent` answered None for it, the dispatcher read that as
    # "already where the work happens", and `sell_surplus` would have stood on the port
    # overworld waiting for a market it never walked into. The lesson is the one already
    # learned for SERVES, arriving a second time by a second route.
    GOALS: tuple = ()          # filled in below, once the goal classes exist

    def __init__(self, *, buy_fn=None, sell_fn=None, sell_down_fn=None,
                 show_grid_fn=None, port_fn=None, context_fn=None, capture_fn=None,
                 tap_fn=None, omni_fn=None, sell_page_fn=None) -> None:
        self._buy = buy_fn
        self._sell = sell_fn
        self._sell_down = sell_down_fn
        self._show_grid = show_grid_fn
        self._port = port_fn
        # The context path. Injectable for the same reason the village's are: the defaults
        # capture and parse, and a test that reaches for the device is a slow test.
        self._context_fn = context_fn
        self._capture = capture_fn
        self._tap = tap_fn
        self._omni = omni_fn
        self._sell_page = sell_page_fn
        self._goal_orders: dict = {}
        from brain.market_state import MarketState
        self._state = MarketState()
        self._tick_frame = None

    # ── the one entry point ──────────────────────────────────────────────────
    def work(self, goal: Any, state: Any) -> ActivityResult:
        where = getattr(state, "state", None) or getattr(state, "location", None)
        if where is not None and where not in self.SERVES:
            # LOCALIZED PERCEPTION ONLY. Being somewhere else is not this activity's problem
            # to solve — it hands back and the dispatcher decides (docs/architecture_DRAFT.md).
            return ActivityResult(UNRECOGNISED, {"state": where},
                                  detail=f"not in a market ({where!r})")

        port = self._port_name(state)

        # THE FRAME BELONGS TO THE TICK, not to whoever asks for it next. Same reason the
        # village keeps one: several readers per tick would otherwise each capture, and each
        # would be answering about a DIFFERENT screen from the one the dispatcher routed on.
        self._tick_frame = getattr(state, "frame", None)

        # SELLING GOES THROUGH THE CONTEXTS — one action per tick, the dispatcher perceives
        # between them, and nothing is swallowed. Buying still calls the old flow; it is the
        # next conversion, and mixing the two for one goal would give the market two owners.
        # AN INJECTED FLOW IS A TEST SEAM, NOT A SECOND PRODUCTION PATH. `run_goal` builds
        # `MarketActivity()` with no arguments, so nothing in production supplies these — a
        # caller that does is substituting the world, the same as `capture_fn` or `tap_fn`.
        # Honouring it keeps the WIRING tests testing wiring (does gather reach the market and
        # report?) instead of forcing them to simulate the market's internals, which would
        # couple them to it. The market's own behaviour is covered directly, by the buy and
        # sell handler tests.
        if isinstance(goal, Hold) and self._buy is None:
            return self._tick(goal, port)
        if isinstance(goal, (FreeHold, SellHold)) and self._sell is None:
            return self._tick(goal, port)
        # THE TRIM GOES THROUGH THE CONTEXTS TOO, and it was the last goal that did not.
        #
        # Without this line `_trim_to` was reached DIRECTLY, so the trim acted on whatever
        # screen happened to be up and the routing in `_on_sell_page` was unreachable. Live
        # 2026-09-07 at Faro, frame 130 of trace_barter_cmd_2026-09-07T15-20-16: the goal
        # arrived while we stood on the PURCHASE page, `market_trim.on_sell_page` read it,
        # `_sell_page` said "this is not the Sell grid", and the leg was blocked four
        # milliseconds after it began — no context line in the log, because `_tick` never ran.
        #
        # `sell_down_to` did its own tab switch, which is why the old `_trim_to` did not need
        # one; the tick version gets it from `_on_purchase_page` instead, one tap and a hand
        # back, exactly like every other goal that finds the wrong grid.
        if isinstance(goal, TrimHold) and self._sell_down is None:
            return self._tick(goal, port)

        if isinstance(goal, Hold):
            return self._buy_toward(goal, port)
        if isinstance(goal, (FreeHold, SellHold)):
            return self._sell_off(getattr(goal, "keep", None) or getattr(goal, "exclude", ()),
                                  port, clear=isinstance(goal, FreeHold))
        if isinstance(goal, TrimHold):
            return self._trim_to(goal, port)
        return ActivityResult(BLOCKED, {}, detail=f"the market cannot serve {goal!r}")

    def on_dialog(self, dialog, goal):
        """First refusal on a dialog covering the market. None means "not mine".

        THE DISPATCHER ASKS THIS BEFORE IT CLASSIFIES A CONTEXT, so a handler table alone is
        not enough — without this method the dispatcher hears "nothing owns it" and falls
        back to `game_rules`, which knows the GAME but not what this activity is in the
        middle of.

        Live 2026-09-06 at Barcelona, the first live run of the buy handler. The cart
        committed 304,767 ducats, the purchase confirm came up, and:

            nothing owns the confirmation dialog — the game rules say 'Ok'
            no rule and no positive option among ['No'] — leaving it alone
            FAILED: gather:Barcelona: a confirmation dialog nobody will answer

        The dialogs were the market's own — its purchase chain — and its `confirm_dialog`,
        `result_dialog` and `negotiation` handlers were never consulted, because the
        dispatcher never reached the context table.

        WHAT IT CLAIMS, AND WHAT IT DOES NOT. Only the cards this activity's own actions
        raise (CLAUDE.md: "who caused the dialog decides how to clear it"). Anything else —
        daily news, a promo, an announcement — is unsolicited, belongs to the dispatcher's
        obstruction layer, and gets None.
        """
        if not isinstance(goal, self.GOALS):
            return None
        where = self._classify()
        handler = self._HANDLERS.get(where)
        if handler is None or where in (_ctx.MARKET_LANDING, _ctx.PURCHASE_PAGE,
                                        _ctx.SELL_PAGE):
            # Not one of our cards. A page is not a dialog, and claiming one here would
            # answer a card we have not identified.
            return None
        _mine = {_ctx.TRADE_GOODS_INFO: (TrimHold, Hold),   # the buy meets this one too
                 _ctx.QUANTITY_DIALOG:  (TrimHold,)}         # only the trim types a figure
        if where in _mine and not isinstance(goal, _mine[where]):
            # THE TRIM'S TWO CARDS — and the BUY meets the first one too. The trim opens a
            # good's card and the keypad over it deliberately; the buy meets the goods card
            # by accident, when `Put In Bulk` is off and a tile tap opens it instead of
            # bulk-loading. Both must answer it, or it stands until the leg dies ("a
            # confirmation dialog nobody will answer", Jakarta 2026-09-08). Under any OTHER
            # goal one of these is a card nobody here opened, and claiming it would be
            # answering a dialog we cannot account for.
            #
            # The KEYPAD stays the trim's alone: it types an exact figure, and the buy takes
            # the whole shelf with `Max`, so a keypad during a buy is a card nobody opened.
            return None
        port = self._port_name(None)
        # AN UNREADABLE PORT NAME IS "UNKNOWN", NOT "SOMEWHERE ELSE". The state is keyed to
        # (goal, port) so one visit's cart never serves another — but a FAILED READING of the
        # name is not a new owner, and treating it as one throws the visit away.
        #
        # Live 2026-09-06 at Madeira: `'port': ''` appears 12 times in the log and the hold
        # was re-read 12 times for 11 purchases. Each flip between ('Hold','Madeira') and
        # ('Hold','') handed back a fresh MarketState with `ledger=None`, so the hold was
        # read again — a trip to the Sell tab, a whole grid scrolled, and a trip back —
        # and the next tick, reading the name successfully, flipped it straight back.
        #
        # The ledger does not need re-reading anyway: it is seeded once and every purchase
        # after that is credited from the shelf drop (user: "if the Result dialog is shown,
        # the number is added, no need to check every time").
        if port:
            self._state = self._state.for_goal((type(goal).__name__, port))
        self._goal_orders = dict(getattr(goal, "orders", {}) or {})
        logger.info(f"[market] the {where} dialog is ours — answering it")
        return handler(self, goal, port)

    # ── the context path ─────────────────────────────────────────────────────
    def _tick(self, goal: Any, port: str) -> ActivityResult:
        """Classify, do ONE thing, hand back. Never a flow.

        An unrecognised screen is handed to the dispatcher rather than acted on — that is
        what stops a dialog being tapped through by something that never knew it was there
        (FC-1, FC-3 in `docs/market_as_contexts.md`).
        """
        import brain.market_context as ctx

        if port:                                  # see `on_dialog` — unknown is not elsewhere
            self._state = self._state.for_goal((type(goal).__name__, port))
        self._goal_orders = dict(getattr(goal, "orders", {}) or {})
        where = self._classify()

        handler = self._HANDLERS.get(where)
        if handler is None:
            # MISS, or a context this goal has no business acting on. Hand back: the
            # dispatcher owns the screen and will clear it or route it.
            return ActivityResult(UNRECOGNISED, {"context": where, "port": port},
                                  detail=f"the market has no move for {where!r}")
        logger.info(f"[market] {where} -> {handler.__name__}")
        return handler(self, goal, port)

    def _on_purchase_page(self, goal: Any, port: str) -> ActivityResult:
        # THE WRONG GRID FOR THIS GOAL IS ONE TAP FROM THE RIGHT ONE. Handing back would be
        # honest and useless — the dispatcher would route here again on the same screen. The
        # two grids look alike and mean opposite things (the shop's stock, the fleet's hold),
        # so the one action worth taking is to switch.
        if not isinstance(goal, Hold):
            # ITS ANSWER IS THE POINT. `ensure_sell_tab` confirms with `_on_sell_tab` and
            # returns False as a REFUSAL — and this discarded it, reporting "switched to the
            # sell tab" whether or not the tab had switched.
            #
            # Live 2026-09-07 at Faro, four times over: tap the Sell row, report the switch,
            # come back to a Purchase page, tap again — "NOTHING CHANGED for 3 ticks (goal=
            # free the hold)". The same shape as `_on_result` claiming a dialog it never
            # pressed: an action reporting a verdict on its own success.
            from actions.buy_materials import ensure_sell_tab
            if not ensure_sell_tab(self._capture_fn(), self._tap_fn()):
                logger.warning("[market] the Sell tab did not open — reporting rather than "
                               "claiming a switch that did not happen")
                return ActivityResult(UNRECOGNISED, self._observed(port),
                                      detail="the Sell tab would not open")
            self._state.did("switched to the sell tab")
            return ActivityResult(WORKING, {**self._observed(port),
                                            "did": "switched to the sell tab"},
                                  detail=f"sell at {port}")
        from brain.activities.market_buy import on_purchase_page

        if self._state.ledger is None:
            # THE HOLD IS READ ON THE SELL PAGE, SO GO THERE AS A TICK, not as a side trip.
            #
            # reading the hold used to switch tabs, scroll a whole grid and switch back INSIDE
            # this tick, which is the sub-loop shape the refactor removes — and it did real
            # damage twice: the live screen no longer matched the frame the tick was reasoning
            # about, so `refresh_market`'s capture found the Sell page and refused ("no
            # restock control"), and every failed port-name read rebuilt the state and made it
            # run again, twelve times for eleven purchases.
            #
            # As a tick it is three plain steps: ask for the Sell tab, read the hold ON that
            # page and seed, come back. Nothing acts on a screen the dispatcher has not seen.
            from actions.buy_materials import ensure_sell_tab
            if not ensure_sell_tab(self._capture_fn(), self._tap_fn()):
                logger.info("[market] the hold is unread and the Sell tab did not open — "
                            "handing back rather than buying against a count we do not have")
                return ActivityResult(UNRECOGNISED, self._observed(port),
                                      detail="the Sell tab would not open")
            self._state.did("went to read the hold")
            return ActivityResult(WORKING, {**self._observed(port),
                                            "did": "went to read the hold"},
                                  detail=f"buy at {port}")
        out = on_purchase_page(self._state, goal, port, frame=self._frame(),
                               capture_fn=self._capture_fn(), tap_fn=self._tap_fn(),
                               omni_fn=self._omni_fn())
        return self._as_result(out, goal, port, what="buy")

    def _seed_from_this_page(self):
        """The hold, from the Sell grid already on screen. No tab switching, no capture.

        `_read_owned_via_sell` does the switching AND the reading; here the switching has
        already happened as its own tick, so only the reading is left — on the frame the
        dispatcher handed us.
        """
        from brain.market_ledger import MarketLedger
        led = MarketLedger()
        try:
            from actions.sell_goods import _sell_page
            goods = _sell_page(self._frame()) or []
            owned = {str(getattr(g, "name", "")).strip().lower(): int(getattr(g, "owned_qty", 0) or 0)
                     for g in goods if getattr(g, "owned_qty", None) is not None}
            if owned:
                led.seed(owned)
                logger.info(f"[market] the hold already carries {owned}")
        except Exception as exc:              # noqa: BLE001 — a poorer seed, not a failure
            logger.debug(f"[market] could not read the hold here: {exc}")
        return led

    def _on_sell_page(self, goal: Any, port: str) -> ActivityResult:
        if isinstance(goal, Hold):
            # WE ARE ON THE PAGE THE HOLD IS LEGIBLE ON, so read it here — the grid is in
            # front of us and this is the only screen that prints a per-good owned count.
            # Then go back. Two actions, two ticks, and no capture the dispatcher has not made.
            if self._state.ledger is None:
                self._state.ledger = self._seed_from_this_page()
            self._show_purchase_grid()
            self._state.did("switched to the purchase tab")
            return ActivityResult(WORKING, {**self._observed(port),
                                            "did": "switched to the purchase tab"},
                                  detail=f"buy at {port}")
        if isinstance(goal, TrimHold):
            return self._trim_to(goal, port)
        from brain.activities.market_sell import on_sell_page

        out = on_sell_page(self._state, goal, frame=self._frame(),
                           capture_fn=self._capture_fn(), tap_fn=self._tap_fn(),
                           omni_fn=self._omni_fn())
        return self._as_result(out, goal, port, what="sell")

    def _as_result(self, out: dict, goal: Any, port: str, *, what: str) -> ActivityResult:
        did = out.get("do")
        if did == "waited":
            # Looked, chose not to act. A tick that taps nothing is a legitimate move when
            # the alternative is a destructive tap — see `market_sell._cart_is_empty`.
            return ActivityResult(WORKING, {**self._observed(port), "did": "looked again"},
                                  detail=f"{what} at {port}")
        if did == "blocked":
            return ActivityResult(BLOCKED, self._observed(port),
                                  detail=out.get("why", f"the {what} page refused"))
        if did == "finished":
            owned_state.changed(owned_state.FLEET, owned_state.BUILDING)
            observed = {**self._observed(port),
                        "stopped_because": out.get("why", "nothing left to do")}
            # THE LOW-STOCK REPORT TRAVELS WITH THE RESULT. `season`/`good` say outright
            # "this port cannot supply this material" — which is what the mission needs to
            # send the fleet somewhere else — and this branch used to drop them, forwarding
            # only the sentence. Live 2026-09-07 the Madeira result reached the runner as
            # {'sold': [], 'port': 'Madeira', 'stopped_because': "'Raisin' is scarce here..."}
            # and the reroute, which reads a different field, had nothing to act on.
            for key in ("season", "good"):
                if out.get(key) is not None:
                    observed[key] = out[key]
            return ActivityResult(FINISHED, observed, detail=f"{what} at {port}")
        return ActivityResult(WORKING, {**self._observed(port), "did": did},
                              detail=f"{what} at {port}")

    def _observed(self, port: str) -> dict:
        """What every result carries: what this visit has done, in the task's vocabulary."""
        out = {"sold": list(self._state.sold), "port": port}
        # WHAT WE JUST DID, in the activity's own words. `did` below is the OUTCOME verb
        # ("refreshed"); this is the ACTION ("tapped the restock"), which is what tells a
        # reader why the dialog now on screen is there. See `Dispatcher._publish_goal`.
        if getattr(self._state, "last_intent", None):
            out["acted"] = str(self._state.last_intent)
        if self._state.ledger is not None:
            try:
                from actions.buy_materials import material_states
                orders = getattr(self._goal_orders, "orders", None) or self._goal_orders
                if orders:
                    out["materials"] = material_states(self._state.ledger, dict(orders))
                out["bought_total"] = sum(v for v in (self._state.ledger.fleet or {}).values())
            except Exception as exc:
                logger.debug(f"[market] could not summarise the ledger: {exc}")
        return out

    def _on_market_landing(self, goal: Any, port: str) -> ActivityResult:
        """Neither grid is up. Open the one THIS GOAL needs — and only that.

        Buying wants the Purchase grid, selling the Sell grid, and opening the wrong one is
        not a harmless extra tap: the two grids look alike and mean opposite things (the shop's
        stock versus the fleet's hold), which is how a Purchase page was once read as the hold.
        """
        if isinstance(goal, Hold):
            self._show_purchase_grid()
            self._state.did("opened the purchase tab")
            return ActivityResult(WORKING, {"port": port, "did": "opened the purchase tab"},
                                  detail=f"buy at {port}")
        from actions.buy_materials import ensure_sell_tab
        if not ensure_sell_tab(self._capture_fn(), self._tap_fn()):
            logger.warning("[market] the Sell tab did not open — reporting rather than "
                           "claiming a switch that did not happen (see `_on_sell_page`)")
            return ActivityResult(UNRECOGNISED, self._observed(port),
                                  detail="the Sell tab would not open")
        self._state.did("opened the sell tab")
        return ActivityResult(WORKING, {"port": port, "did": "opened the sell tab"},
                              detail=f"sell at {port}")

    def _show_purchase_grid(self) -> None:
        if self._show_grid is not None:
            self._show_grid()
            return
        _default_show_purchase_grid()

    def _on_our_dialog(self, goal: Any, port: str) -> ActivityResult:
        """OUR OWN card — complete it with ONE tap and hand back.

        Never a loop. `commit_via_positive_taps` presses until the cycle closes, and that is
        how FC-3 happened at San Village: iteration 1 pressed OK, the overflow card appeared,
        iteration 2 pressed its Receive, and the handler that owns overflow never ran.
        """
        from brain.commit_actions import tap_one_positive
        if not tap_one_positive(goal_keywords=["ok", "confirm"],
                                capture_fn=lambda: self._frame(),
                                tap_fn=self._tap_fn()):
            # See `_on_result`: claiming a dialog we did not press ends the dispatcher's turn
            # on it, so its fallbacks never run and the card stands.
            logger.info("[market] this dialog has no positive button — handing back rather "
                        "than reporting one we did not answer")
            return ActivityResult(UNRECOGNISED, {"port": port},
                                  detail="our dialog, with no positive button")
        self._state.did("answered a dialog")
        return ActivityResult(WORKING, {"port": port, "did": "answered a dialog"},
                              detail=f"sell at {port}")

    def _on_restock_prompt(self, goal: Any, port: str) -> ActivityResult:
        """The Replenish-Stock card our own ↻ raised — OK.

        WE ASKED FOR IT, so we finish it (CLAUDE.md: "who caused the dialog decides how to
        clear it"). It spends a BLUE gem, and the price was already confirmed as blue before
        the control was tapped — `_tap_the_restock_control` refuses anything else, and red
        gems are real money.

        THIS CONTEXT WAS CLASSIFIED AND UNHANDLED for as long as the table has existed,
        because `refresh_market` answered the card itself: capture, OCR for a word that looks
        like OK, tap it, capture again to verify, and up to 90 seconds of sleeping if the
        timer was nearly up. A whole flow inside one tick, with its own OCR, beside a context
        the dispatcher was already naming correctly.
        """
        from brain.commit_actions import tap_one_positive
        if not tap_one_positive(goal_keywords=["ok", "confirm"],
                                capture_fn=lambda: self._frame(),
                                tap_fn=self._tap_fn()):
            logger.info("[market] the Replenish card has no positive button — handing back "
                        "rather than reporting one we did not answer")
            return ActivityResult(UNRECOGNISED, self._observed(port),
                                  detail="a Replenish card with no positive button")
        logger.info("[market] Replenish Stock — OK (a blue gem, already confirmed)")
        self._state.did("answered the restock prompt")
        return ActivityResult(WORKING, {**self._observed(port),
                                        "did": "answered the restock prompt"},
                              detail=f"buy at {port}")

    def _on_cargo_full_notice(self, goal: Any, port: str) -> ActivityResult:
        """"The Cargo Hold's Trade Goods slot will be exceeded by N slots. Purchase?" — OK.

        A PLAIN ACKNOWLEDGEMENT, NOT A REFUSAL. The game hands back what fits and the rest is
        not taken; it spends no gems, so OK is the answer (user, 2026-09-04: "for this one you
        can tap the Ok button"). Cancelling abandons a purchase the hold has room for.

        Ported from `_react_after_purchase`, which matched BOTH phrases and never the bare
        word "notice" — a Notice is a shape, not a meaning. Missing it once already cost a
        whole purchase: live 2026-09-04 at Madeira (frame 272) the dialog was left standing,
        `purchase_goods` returned ok with purchased=False, and the caller walked out of the
        market through the chromed title with 105 slots free and nothing bought.
        """
        from brain.commit_actions import tap_one_positive
        if not tap_one_positive(goal_keywords=["ok"], capture_fn=lambda: self._frame(),
                                tap_fn=self._tap_fn()):
            logger.warning("[market] the overload Notice is up but its OK could not be "
                           "found — handing back rather than tapping blind")
            return ActivityResult(UNRECOGNISED, self._observed(port),
                                  detail="an overload Notice with no readable OK")
        logger.info("[market] trade-goods overload Notice — OK (the hold takes what fits)")
        self._state.did("acknowledged the overload notice")
        return ActivityResult(WORKING, {**self._observed(port),
                                        "did": "acknowledged the overload notice"},
                              detail=f"buy at {port}")

    def _on_negotiation(self, goal: Any, port: str) -> ActivityResult:
        """The haggle prompt — DECLINED, which is not the positive option.

        THE ONLY MARKET CARD WHOSE RIGHT ANSWER IS "NO". Every other one is completed by its
        positive button; this one offers to gamble the transaction on a haggle, and the flow
        this replaces has always skipped it:

            if "negotiat" in txt:                          # haggle popup -> skip
                pos = find_text_button(tokens, "no", min_ratio=0.85)
                logger.info("[buy] negotiation popup — No")

        Routing it to the positive handler, as this table did until now, would have said YES
        on every purchase — a behaviour change smuggled in by a refactor that is supposed to
        preserve behaviour. It also explains the stall at Barcelona: `game_rules` was offered
        a card whose only option it could read was 'No', found nothing positive, and quite
        correctly refused to answer someone else's dialog.
        """
        from actions.sail_actions import _ocr_frame
        from actions.route_execution import find_text_button
        tokens = _ocr_frame(self._frame(), min_conf=0.3)
        where = find_text_button(tokens, "no", min_ratio=0.85)
        if where is None:
            # Not answerable this tick. Hand back rather than press something else — the
            # dispatcher looks again, and a wrong button here accepts a haggle.
            return ActivityResult(UNRECOGNISED, self._observed(port),
                                  detail="a negotiation prompt with no readable 'No'")
        logger.info(f"[market] negotiation prompt — No")
        self._tap_fn()(*where)
        self._state.did("declined the negotiation")
        return ActivityResult(WORKING, {**self._observed(port),
                                        "did": "declined the negotiation"},
                              detail=f"buy at {port}")

    def _on_result(self, goal: Any, port: str) -> ActivityResult:
        """THE PROOF a transaction happened — and the ONLY place the ledger is written.

        FC-2 recorded a purchase that never happened: no result card, no goods, an entry
        anyway. Writing only from here makes that unreachable, because this context exists
        only when the game says the trade is done.
        """
        from brain.commit_actions import tap_one_positive
        sold = self._read_result_goods()
        # A SELL CARD NAMES NO GOODS. It reports money — Mate Trade EXP, Sales Cost, Tax,
        # Surcharge, Profit, Total Amount, Balance — and `_read_result_goods` looks for a
        # goods grid that is not on it, so this list is always empty on a sale. Live
        # 2026-09-07 at London every result carried `'sold': []` through a 122,637,216-ducat
        # sale of 4,382 Bambara Groundnut.
        #
        # That is not only a false report. `market_sell.on_sell_page` refuses to call a clear
        # finished while `held and not state.sold` — the guard written after Lisboa on
        # 2026-09-06, where a mission reported success holding the 3,668 units it had sailed
        # there to sell — and a `sold` that can never fill is a guard that can never fire.
        #
        # So the names come from what was staged, and the CARD is still what authorises them:
        # being in this handler means the game has confirmed the transaction.
        sold = list(sold) + [n for n in self._state.sold_pending if n not in sold]
        for name in sold:
            if name not in self._state.sold:
                self._state.sold.append(name)
        self._state.sold_pending.clear()
        if not tap_one_positive(goal_keywords=["ok", "confirm"],
                                capture_fn=lambda: self._frame(),
                                tap_fn=self._tap_fn()):
            # NOTHING WAS PRESSED, SO NOTHING WAS CLEARED. Saying otherwise ends the
            # dispatcher's turn on this dialog — `_offer_dialog` returns as soon as an
            # activity claims it — so its own fallbacks never run and the card just stands.
            #
            # Live 2026-09-06 at Barcelona, five times: "no positive button found — settled
            # after 0 tap(s)" then "market answered the informational dialog -> working
            # {'did': 'cleared the result dialog'}", and the task stopped with NOTHING
            # CHANGED for 3 ticks. The goods HAD been read and recorded above, which is why
            # this went unnoticed — the ledger was right and only the screen was stuck.
            #
            # A result card with no positive button is a real shape: some carry only an X.
            # Handing back says so honestly and lets the dispatcher close it.
            logger.info("[market] the result card has no positive button — handing back "
                        "rather than reporting a dialog we did not clear")
            return ActivityResult(UNRECOGNISED, {"sold": list(self._state.sold), "port": port},
                                  detail="a result card with no positive button")
        self._state.did("cleared the result dialog")
        return ActivityResult(WORKING, {"sold": list(self._state.sold), "port": port,
                                        "did": "cleared the result dialog"},
                              detail=f"sell at {port}")

    def _read_result_goods(self) -> list:
        """What the result card says actually sold. Best effort; never raises into a tick."""
        try:
            from actions.sell_goods import _sell_page
            goods = _sell_page(self._frame())
            return [str(getattr(g, "name", "")) for g in (goods or ())
                    if getattr(g, "name", None)]
        except Exception as exc:
            logger.debug(f"[market] could not read the result card: {exc}")
            return []

    # ── the readings, all injectable ─────────────────────────────────────────
    def _classify(self) -> str:
        if self._context_fn is not None:
            return self._context_fn(self._frame())
        import brain.market_context as ctx
        return ctx.classify(self._frame())

    def on_tick_frame(self, frame) -> None:
        """The dispatcher's frame for THIS tick, handed over before `on_dialog`.

        `work()` sets this itself; `on_dialog` runs earlier in the same tick and would
        otherwise classify the previous screen. Same frame either way — one capture per tick.
        """
        self._tick_frame = frame

    def _frame(self):
        if self._tick_frame is not None:
            return self._tick_frame
        return self._capture_fn()()

    def _capture_fn(self):
        if self._capture is not None:
            return self._capture
        from capture.adb_capture import capture_screen
        return capture_screen

    def _tap_fn(self):
        if self._tap is not None:
            return self._tap
        from actions.adb_actions import tap
        return tap

    def _omni_fn(self):
        if self._omni is not None:
            return self._omni
        from vision.omniparser import parse_fast_cached
        return parse_fast_cached

    # ── the three operations ─────────────────────────────────────────────────
    def _buy_toward(self, goal: Hold, port: str) -> ActivityResult:
        show_grid, buy = self._show_grid, self._buy
        if show_grid is None:
            show_grid = _default_show_purchase_grid
        if buy is None:
            from actions.buy_materials import buy_to_goal as buy

        show_grid()
        # Each round buys about one shelf, or spends blue gems refreshing a sold-out one
        # rather than waiting ~20 minutes for the timer. The loop stops as soon as the goal
        # is met, so this bound is only a backstop against a tiny-stock/huge-goal case
        # burning gems without end (user 2026-08-18: "just raise the bound").
        wanted = sum(goal.orders.values())
        max_rounds = min(60, max(4, -(-wanted // 20)))
        res = buy(port, dict(goal.orders), max_rounds=max_rounds) or {}
        # ACTING IS THE OTHER WAY DATA GOES BAD. The hold moved and so did the shelf; anything
        # remembered about either is now a description of a screen that no longer exists.
        owned_state.changed(owned_state.FLEET, owned_state.BUILDING)
        bought = int(res.get("bought_total") or 0)
        met = bool(res.get("met"))
        # WHY IT STOPPED, in the task's terms. "Met" and "the shelf ran out" are different
        # situations and only one of them is worth reacting to.
        because = "goal met" if met else ("shelf empty or hold full" if bought == 0
                                          else "no further progress")
        logger.info(f"[market] {goal} at {port}: bought {bought}, {because}")

        # BLOCKED FOR SPACE, STANDING WHERE SPACE IS MADE (user, 2026-09-01).
        #
        # A buy that bought nothing is out of shelf or out of hold, and only one of those can
        # be fixed from here — but it can be fixed COMPLETELY, because the market that will
        # not take our money will happily take our cargo. Live 2026-09-01 the fleet sat in
        # Tripoli's market, flipped to the Sell page to settle the ledger, read
        # `Bambara Groundnut 4,448 — 98% — profitable`, went back to Purchase, and failed the
        # leg for want of space. The mission graph does have a `sell_surplus` leg — scheduled
        # AFTER this gather, which is the deadlock: the gather needs room, and the step that
        # makes room waits on the gather.
        #
        # Selling here is SUPPORT, not a leg: it protects the materials this order wants plus
        # the supplies, sells only what is PROFITABLE, and never moves the fleet. If it frees
        # anything we stay BLOCKED on purpose, so the task re-dispatches the buy against a
        # hold that now has room rather than this handler growing a retry loop of its own.
        freed: list = []
        if not met and not bought:
            protect = list(goal.orders) + _SUPPLIES
            try:
                relief = self._sell_off(protect, port, clear=False)
                freed = list((relief.observed or {}).get("sold") or [])
            except Exception as exc:                  # noqa: BLE001 — support, never fatal
                logger.warning(f"[market] could not sell surplus to make room: {exc}")
            if freed:
                logger.info(f"[market] sold surplus at {port} to make room: {freed} — the "
                            f"buy is re-dispatched against a hold that now has space")
                owned_state.changed(owned_state.FLEET, owned_state.BUILDING)

        return ActivityResult(
            FINISHED if (met or bought) else BLOCKED,
            # NO PER-GOOD BREAKDOWN OF WHAT WAS BOUGHT, because there is none to give:
            # `buy_to_goal` counts a total across the whole order and the bulk control buys
            # by the shelf, not by the line. Reporting `{good: bought}` for a multi-good
            # order would be inventing a number — it is exactly the Barcelona attribution
            # that credited Iron's +324 to Matchlock Gun as well.
            #
            # `materials` IS NOT THAT NUMBER, and the difference is the whole point. It says
            # where each material STANDS — met / short / unknown, from the ledger's per-good
            # belief — which is knowable even when the round's attribution is not, and says
            # "unknown" precisely where it is not. A total cannot answer "is Iron at target?"
            # and the ledger can, so the mission stops having to settle a leg on a
            # whole-order verdict that no single port can satisfy.
            {"bought_total": bought, "ordered": dict(goal.orders),
             "met": met, "materials": dict(res.get("materials") or {}),
             "port": port, "stopped_because": because,
             "freed_for_space": freed},
            detail=str(goal))

    def _trim_to(self, goal: TrimHold, port: str) -> ActivityResult:
        """One move on the Sell grid toward the keep levels. The dispatcher calls again.

        An INJECTED `sell_down` still runs the whole walk in one call — that is how the
        callers that are not on the tick path use it, and their tests with it.
        """
        if self._sell_down is not None:
            return self._trim_in_one_call(goal, port)

        from brain.activities import market_trim
        out = market_trim.on_sell_page(self._state, goal, frame=self._frame(),
                                       tap_fn=self._tap_fn(), omni_fn=self._omni_fn())
        return self._trim_result(out, goal, port)

    def _trim_result(self, out: dict, goal: TrimHold, port: str) -> ActivityResult:
        did = out.get("do")
        if did == "blocked":
            return ActivityResult(BLOCKED, self._observed(port),
                                  detail=out.get("why", "the trim refused"))
        if did == "unclaimed":
            # A CARD WE DID NOT OPEN. Hand it back rather than answering it — the dispatcher
            # owns the screen and knows what else might want it.
            return ActivityResult(UNRECOGNISED, self._observed(port),
                                  detail=out.get("why", "not this trim's dialog"))
        if did == "dismiss":
            # Close the card through its OWN control. An outside tap does dismiss a dialog we
            # opened, but it is the same gesture as a misfire and so unreadable in the log.
            from brain.commit_actions import tap_one_positive
            tap_one_positive(goal_keywords=["cancel", "close"],
                             capture_fn=lambda: self._frame(), tap_fn=self._tap_fn())
            self._state.did("closed the goods card")
            return ActivityResult(WORKING, {**self._observed(port),
                                            "did": "closed the goods card"},
                                  detail=out.get("why", "nothing to stage here"))
        if did == "finished":
            owned_state.changed(owned_state.FLEET, owned_state.BUILDING)
            # BOTH SIDES COERCED. `_why_nothing_to_stage` returns a LIST and `trim_skipped`
            # is one too, so `(list or ()) + tuple(...)` raised TypeError — and only when
            # something had actually been skipped, which is why the unit tests and the first
            # half of the run went by without it. Live 2026-09-07 at London: the trim sold
            # its 731 Pig, restored bulk, and died on the finishing tick with "can only
            # concatenate list (not "tuple") to list", leaving the phone idle.
            for skip in tuple(out.get("skipped") or ()) + tuple(self._state.trim_skipped):
                # A SKIP IS A READING FAILURE, AND IT MUST NOT BE SILENT. Refusing to sell a
                # good whose owned quantity could not be read is right — nothing downstream
                # can tell a guess from a count — but dropping the reason made a trim that
                # never had a number for Candle look like one that decided Candle was fine.
                logger.warning(f"[market] trim skipped at {port}: {skip}")
            trimmed = dict(self._state.trim_staged)
            logger.info(f"[market] trimmed at {port}: {trimmed or 'nothing'}")
            return ActivityResult(FINISHED,
                                  {"trimmed": trimmed, "port": port,
                                   "stopped_because": out.get("why") or "nothing to trim"},
                                  detail=str(goal))
        return ActivityResult(WORKING,
                              {**self._observed(port), "did": out.get("why", "trimming")},
                              detail=f"trim at {port}")

    def _trim_in_one_call(self, goal: TrimHold, port: str) -> ActivityResult:
        """The injected `sell_down_to`, kept whole for the callers that are not ticks."""
        res = self._sell_down(port, dict(goal.keep_qty)) or {}
        owned_state.changed(owned_state.FLEET, owned_state.BUILDING)
        logger.info(f"[market] trimmed at {port}: {res.get('reason')}")
        for skip in res.get("skipped") or ():
            logger.warning(f"[market] trim skipped at {port}: {skip}")
        return ActivityResult(
            FINISHED if res.get("ok") else BLOCKED,
            {"trimmed": res.get("trimmed"), "port": port,
             "stopped_because": res.get("reason") or "nothing to trim"},
            detail=str(goal))

    def _on_goods_info(self, goal: Any, port: str) -> ActivityResult:
        """The Trade Goods Info card — the trim opens one deliberately, the buy meets one by
        accident when `Put In Bulk` is off, and both have to answer it."""
        if isinstance(goal, Hold):
            from brain.activities.market_buy import on_goods_info
            out = on_goods_info(self._state, goal, frame=self._frame(),
                                tap_fn=self._tap_fn(), omni_fn=self._omni_fn())
            return self._as_result(out, goal, port, what="buy")
        if not isinstance(goal, TrimHold):
            return ActivityResult(UNRECOGNISED, self._observed(port),
                                  detail="a goods card this goal did not open")
        from brain.activities import market_trim
        out = market_trim.on_goods_info(self._state, goal, frame=self._frame(),
                                        tap_fn=self._tap_fn(), omni_fn=self._omni_fn())
        return self._trim_result(out, goal, port)

    def _on_keypad(self, goal: Any, port: str) -> ActivityResult:
        """The number keypad, open over a goods card. Again, the trim's."""
        if not isinstance(goal, TrimHold):
            return ActivityResult(UNRECOGNISED, self._observed(port),
                                  detail="a keypad this goal did not open")
        from brain.activities import market_trim
        out = market_trim.on_keypad(self._state, goal, frame=self._frame(),
                                    capture_fn=self._capture_fn(), tap_fn=self._tap_fn(),
                                    omni_fn=self._omni_fn())
        return self._trim_result(out, goal, port)

    def _sell_off(self, protect: Sequence[str], port: str, *, clear: bool) -> ActivityResult:
        sell = self._sell
        if sell is None:
            from actions.sell_goods import sell_goods as sell
        kw = {"goal": "clear", "keep": list(protect)} if clear else {"exclude": list(protect)}
        res = sell(port, **kw) or {}
        owned_state.changed(owned_state.FLEET, owned_state.BUILDING)
        sold = list(res.get("sold") or [])
        logger.info(f"[market] {'freed' if clear else 'sold'} at {port}: {sold or 'nothing'}")
        # A CLEAR THAT NEVER READ THE HOLD IS NOT A FINISHED CLEAR. This returned FINISHED
        # unconditionally, so `sell_goods` refusing to report the hold ("could not reach the
        # Sell grid") still came back as a completed leg. Live 2026-09-01 that marked
        # `trim_before_gather` done at Luanda having sold nothing, and the mission gathered
        # into a hold still carrying 4,448 Bambara Groundnut — wedging two legs later at
        # Tripoli with the cargo bar red. `ok` is False only when the flow REFUSED; a genuine
        # empty hold still finishes.
        ok = res.get("ok")
        return ActivityResult(
            FINISHED if ok is not False else BLOCKED,
            {"sold": sold, "port": port,
             "stopped_because": res.get("reason") or "nothing left to sell"},
            detail=f"{'free' if clear else 'sell'} at {port}")

    # ── helpers ──────────────────────────────────────────────────────────────
    def _port_name(self, state: Any) -> str:
        """Which port this market belongs to — ASKED OF THE PLACE, not of the screen.

        THE NAME IS PAINTED ON THE OVERWORLD AND NOWHERE ELSE. Inside a building the title
        bar is the SUB-MENU, so a read here cannot succeed — `observation` states it as
        `SETTLEMENT_NAME_VISIBLE_ON = {"port_overworld"}` and records the cost of forgetting
        it: "Live 2026-08-22: the mission asked for the port from the MAIN MENU, retried
        three times, and aborted 'current port unreadable'".

        This method made that same mistake in a new place. `_current_port()` gates on
        `location == "port_overworld"`, so from a market it returns None every time — after
        three captures and two seconds of sleeping — and the caller got "".

        A BUILDING BELONGS TO A PORT, which is the lifetime the answer already has
        (CLAUDE.md: COMPANY > FLEET > PLACE > BUILDING > PANEL, and the port IS the PLACE).
        `last_known_settlement` is exactly that value: written when the overworld paints it,
        carried through the buildings above it, and dropped on reaching sea — "at sea —
        forgetting 'Madeira'; a port we have left is not where we are". A village needs no
        special case here: it sits on the sea and has no market.

        What that empty string cost, live 2026-09-06 at Madeira: the market's state is
        keyed to (goal, port), so "" flipped the key on 12 of 27 ticks, handed back a fresh
        MarketState each time, and the hold was re-read — Sell tab, full scroll, back again —
        12 times for 11 purchases.
        """
        if self._port is not None:
            return self._port()
        got = getattr(state, "port", None)
        if got:
            return got
        try:
            from brain import observation as _obs
            cur = _obs.current()
            held = (cur.last_known_settlement if cur else None) \
                or _obs._ensure_persisted_loaded()
            if held:
                return str(held)
        except Exception as exc:              # noqa: BLE001 — a poorer answer, not a failure
            logger.debug(f"[market] could not resolve the port from the place: {exc}")
        return ""


MarketActivity.GOALS = (Hold, FreeHold, TrimHold, SellHold)

# ── the handler table ────────────────────────────────────────────────────────
#
# Classify the context, look up the handler, do ONE thing, hand back. The same shape as
# `WorldMapActivity._HANDLERS` and `VillageActivity._HANDLERS`, and the reason a dialog can
# no longer be tapped through by code that never knew it was there.
#
# A context with NO entry is handed back on purpose. `quantity_dialog`, `trade_goods_info`,
# `restock_prompt`, `overflow_prompt` and `discard_notice` belong to flows this activity does
# not drive yet (buying, and the barter's overflow, which is the village's). Answering them
# here would be this activity acting on a screen it has no business deciding about — the
# thing `MISS` exists to prevent.
import brain.market_context as _ctx  # noqa: E402  (after the class, like the village's)

MarketActivity._HANDLERS = {
    _ctx.SELL_PAGE:       MarketActivity._on_sell_page,
    _ctx.PURCHASE_PAGE:   MarketActivity._on_purchase_page,
    _ctx.MARKET_LANDING:  MarketActivity._on_market_landing,
    _ctx.CONFIRM_DIALOG:  MarketActivity._on_our_dialog,
    _ctx.RESULT_DIALOG:   MarketActivity._on_result,
    _ctx.NEGOTIATION:     MarketActivity._on_negotiation,
    _ctx.CARGO_FULL_NOTICE: MarketActivity._on_cargo_full_notice,
    _ctx.RESTOCK_PROMPT:  MarketActivity._on_restock_prompt,
    # The trim's own two screens — the good's card and the keypad over it. Every other goal
    # answers UNRECOGNISED on them, which is what it means to open a dialog deliberately.
    _ctx.TRADE_GOODS_INFO: MarketActivity._on_goods_info,
    _ctx.QUANTITY_DIALOG:  MarketActivity._on_keypad,
}


def _default_show_purchase_grid() -> None:
    """Open the Purchase tab. The market opens on its greeting page, not the goods grid."""
    from actions.adb_actions import tap
    from actions.market_actions import MARKET_COORDS
    tap(*MARKET_COORDS["purchase"])
    import time
    time.sleep(2.0)
