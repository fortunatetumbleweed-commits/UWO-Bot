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
from brain.dispatcher import ActivityResult, BLOCKED, FINISHED, UNRECOGNISED


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
                 show_grid_fn=None, port_fn=None) -> None:
        self._buy = buy_fn
        self._sell = sell_fn
        self._sell_down = sell_down_fn
        self._show_grid = show_grid_fn
        self._port = port_fn

    # ── the one entry point ──────────────────────────────────────────────────
    def work(self, goal: Any, state: Any) -> ActivityResult:
        where = getattr(state, "state", None) or getattr(state, "location", None)
        if where is not None and where not in self.SERVES:
            # LOCALIZED PERCEPTION ONLY. Being somewhere else is not this activity's problem
            # to solve — it hands back and the dispatcher decides (docs/architecture_DRAFT.md).
            return ActivityResult(UNRECOGNISED, {"state": where},
                                  detail=f"not in a market ({where!r})")

        port = self._port_name(state)
        if isinstance(goal, Hold):
            return self._buy_toward(goal, port)
        if isinstance(goal, FreeHold):
            return self._sell_off(goal.keep, port, clear=True)
        if isinstance(goal, SellHold):
            return self._sell_off(goal.exclude, port, clear=False)
        if isinstance(goal, TrimHold):
            return self._trim_to(goal, port)
        return ActivityResult(BLOCKED, {}, detail=f"the market cannot serve {goal!r}")

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
        sell_down = self._sell_down
        if sell_down is None:
            from actions.sell_goods import sell_down_to as sell_down
        res = sell_down(port, dict(goal.keep_qty)) or {}
        owned_state.changed(owned_state.FLEET, owned_state.BUILDING)
        logger.info(f"[market] trimmed at {port}: {res.get('reason')}")
        # A SKIP IS A READING FAILURE, AND IT WAS SILENT. `sell_down_to` refuses to sell a
        # good whose owned quantity it could not read — right, because nothing downstream can
        # tell a guess from a count — and returns why. Dropping that made the trim look like
        # it had simply decided Candle was fine, when in fact it never had a number for it.
        for skip in res.get("skipped") or ():
            logger.warning(f"[market] trim skipped at {port}: {skip}")
        return ActivityResult(
            FINISHED if res.get("ok") else BLOCKED,
            {"trimmed": res.get("trimmed"), "port": port,
             "stopped_because": res.get("reason") or "nothing to trim"},
            detail=str(goal))

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
        if self._port is not None:
            return self._port()
        got = getattr(state, "port", None)
        if got:
            return got
        from brain.barter_mission_live import _current_port
        return _current_port() or ""


MarketActivity.GOALS = (Hold, FreeHold, TrimHold, SellHold)


def _default_show_purchase_grid() -> None:
    """Open the Purchase tab. The market opens on its greeting page, not the goods grid."""
    from actions.adb_actions import tap
    from actions.market_actions import MARKET_COORDS
    tap(*MARKET_COORDS["purchase"])
    import time
    time.sleep(2.0)
