# actions/buy_materials.py
# #23 — buy-materials executor, PERCEIVE → ACTION → PERCEIVE (no subloops).
#
# Rewritten 2026-08-15 (feedback_no_subloops_perceive_act_perceive): the old version
# wrapped market_actions.buy_goods, which taps a tile then BLOCKS in a subloop waiting
# for a specific "Trade Goods Info" dialog — when the market instead loaded goods
# straight into the cart, that subloop timed out and aborted with goods unbought.
#
# This version never assumes a fixed flow:
#   PERCEIVE  the Purchase screen (OmniParser — chromed screen).
#   ACTION    tap every wanted material tile (one batch; bulk-loads into the cart).
#   PERCEIVE  did the cart change? (the yellow Purchase commit now shows a cost).
#   ACTION    tap Purchase (ducats/blue-gem only, NEVER red-gem).
#   PERCEIVE  handle whatever appears next (a negotiation popup → skip), confirm.
#
# Quantities are NOT exact: tapping a tile bulk-loads the available stock. The barter
# good has huge profit, so a little leftover material is fine — we buy what's there.

from __future__ import annotations

import time
from typing import Callable, Mapping, Optional

from loguru import logger


def _find_material_tile(elements, material: str):
    """(cx, cy) of the goods-grid tile whose label matches `material` (OmniParser
    button in the left/centre grid, cx < ~1600). None if not on screen (out of
    stock / wrong tab)."""
    m = material.lower()
    best = None
    for e in elements:
        lab = (getattr(e, "label", "") or "").strip().lower()
        if not lab or getattr(e, "cx", None) is None:
            continue
        if getattr(e, "cx") >= 1600:
            continue
        et = (getattr(e, "element_type", "") or "")
        if m in lab or lab in m:
            if et == "button" or best is None:
                best = (int(e.cx), int(e.cy))
    return best


def _find_purchase_commit(frame, elements):
    """The yellow Purchase commit button (detect_commit_buttons), or None. Carries
    verb/cost/currency so the caller can gate on currency + confirm a cost>0 load."""
    try:
        from vision.region_detectors.commit_button import detect_commit_buttons
        commits = detect_commit_buttons(elements, frame)
    except Exception as exc:
        logger.debug(f"[buy] commit detect failed: {exc}")
        return None
    for c in commits:
        if "purchas" in (getattr(c, "verb", "") or "").lower():
            return c
    return commits[0] if commits else None


def _cost_of(commit) -> int:
    raw = str(getattr(commit, "cost", "") or "").replace(",", "").strip()
    return int(raw) if raw.isdigit() else 0


def purchase_goods(port: str, goal: Optional[Mapping[str, int]] = None, *,
                   goods: Optional[list] = None,
                   capture_fn: Optional[Callable] = None,
                   tap_fn: Optional[Callable] = None,
                   omni_fn: Optional[Callable] = None,
                   commit_fn: Optional[Callable] = None,
                   settle: float = 1.2) -> dict:
    """Unified perceive-act-perceive purchase (step 1 of the consolidation —
    project_unified_purchase_goal_design).

    goal: {good: target_units} to buy TOWARD (e.g. {'Coral': 1000}); can be multiple
          goods. When goal is empty/None, the default is LOAD AS MUCH AS POSSIBLE of
          `goods` (the old trading/auto_buy 'max' behaviour). Quantities are inexact
          by design (bulk + reserve; the barter good has huge profit). Precise capping
          for very-large-stock goods + the restock-timer strategy are STEP 2.

    Returns {ok, goal, tapped, purchased, cost, not_found, reason}."""
    targets = dict(goal) if goal else {g: None for g in (goods or [])}
    targets = {g: q for g, q in targets.items() if q is None or q > 0}
    if not targets:
        return {"ok": True, "goal": dict(goal or {}), "tapped": [], "purchased": False,
                "cost": 0, "reason": "nothing requested"}

    if capture_fn is None:
        from capture.adb_capture import capture_screen
        capture_fn = capture_screen
    if tap_fn is None:
        from actions.adb_actions import tap as tap_fn
    if omni_fn is None:
        from vision.omniparser import parse_fast_cached
        omni_fn = parse_fast_cached
    if commit_fn is None:
        commit_fn = _find_purchase_commit

    # PERCEIVE the market.
    frame0 = capture_fn()
    els = omni_fn(frame0)

    # Bulk mode ON → one tile-tap loads the good's available stock straight into the
    # cart (the proven direct-load path; no fragile quantity dialog). Best-effort.
    try:
        from actions.market_actions import _ensure_bulk_mode
        _ensure_bulk_mode(True, frame0)
        time.sleep(0.5)
        els = omni_fn(capture_fn())
    except Exception as exc:
        logger.debug(f"[buy] bulk-mode ensure skipped: {exc}")

    # ACTION: tap each wanted good's tile (batch) → bulk-loads into the cart.
    tapped, not_found = [], []
    for material in targets:
        tile = _find_material_tile(els, material)
        if tile:
            logger.info(f"[{port}] load {material} — tap tile @ {tile}")
            tap_fn(*tile)
            tapped.append(material)
            time.sleep(settle)
        else:
            not_found.append(material)
    if not tapped:
        return {"ok": False, "goal": dict(goal or {}), "tapped": [],
                "purchased": False, "cost": 0,
                "reason": f"no tiles found for {not_found} (out of stock / wrong tab)"}

    # PERCEIVE: did the cart change? The Purchase commit now carries a cost.
    time.sleep(settle)
    frame2 = capture_fn()
    commit = commit_fn(frame2, omni_fn(frame2))
    if commit is None:
        return {"ok": False, "goal": dict(goal or {}), "tapped": tapped,
                "purchased": False, "cost": 0,
                "reason": "no Purchase button after loading — cart may be empty"}
    if getattr(commit, "currency", None) == "red_gem":
        return {"ok": False, "goal": dict(goal or {}), "tapped": tapped,
                "purchased": False, "cost": _cost_of(commit), "refused": True,
                "reason": "Purchase cost is red gems (real money) — refused"}
    cost = _cost_of(commit)

    # ACTION: tap Purchase (opens a Confirm Purchase dialog).
    logger.info(f"[{port}] tap Purchase @ ({commit.cx},{commit.cy}) cost={cost}")
    from memory.observed_facts import forget
    forget("hold")   # the hold is about to change — never serve a stale one
    tap_fn(commit.cx, commit.cy)
    time.sleep(settle + 1.0)

    # PERCEIVE → REACT to whatever the game shows next (no assumed sequence):
    #   Confirm Purchase dialog → OK; then an Attempt-Negotiation popup → No.
    confirmed = False
    try:
        confirmed = _react_after_purchase(capture_fn, tap_fn)
    except Exception as exc:
        logger.debug(f"[buy] post-purchase react skipped: {exc}")

    return {"ok": True, "goal": dict(goal or {}), "tapped": tapped,
            "purchased": confirmed, "confirmed": confirmed, "cost": cost,
            "not_found": not_found,
            "reason": f"{'bought' if confirmed else 'loaded (confirm pending?)'} "
                      f"{tapped} (cost {cost})"}


def buy_materials_at_port(orders: Mapping[str, int], port: str, **kw) -> dict:
    """Barter-materials buy = purchase_goods with a GOAL (the required quantities).
    Thin wrapper over the unified purchase; carries `requested` for callers."""
    res = purchase_goods(port, goal=orders, **kw)
    res["requested"] = dict(orders or {})
    return res


def _react_after_purchase(capture_fn, tap_fn) -> bool:
    """Perceive the screen after tapping Purchase and react to whichever dialog is up.
    Returns True once the Confirm Purchase 'OK' was tapped. NOT a subloop — a few
    perceive→act steps, each dispatching on what's actually there."""
    from actions.sail_actions import _ocr_frame
    from actions.route_execution import find_text_button

    confirmed = False
    # The post-purchase chain can be: Confirm Purchase → (Attempt Negotiation) →
    # Result summary — each a dialog. Perceive and dispatch on WHAT'S THERE, a few
    # bounded steps; break when no dialog remains. Not a wait-for-state subloop.
    for _ in range(4):
        time.sleep(1.0)
        tokens = _ocr_frame(capture_fn(), min_conf=0.3)
        txt = " ".join(t.lower() for t, _c, _x, _y in tokens)
        if "negotiat" in txt:                          # haggle popup → skip
            pos = find_text_button(tokens, "no", min_ratio=0.85)
            if pos:
                logger.info("[buy] negotiation popup — No")
                tap_fn(*pos)
                continue
        if "confirm" in txt or "result" in txt or "balance" in txt:  # buy OR sell
            pos = find_text_button(tokens, "ok", min_ratio=0.85)
            if pos:
                logger.info(f"[txn] dialog OK (confirm={'confirm' in txt}, "
                            f"result={'result' in txt})")
                tap_fn(*pos)
                confirmed = True
                continue
        break
    return confirmed


# Shared post-transaction dialog handler (buy + sell both hit Confirm → Result →
# maybe Negotiation). Exposed under a neutral name for the sell flow to reuse.
react_after_commit = _react_after_purchase


# ── Blue-gem market refresh (restock) + goal-driven buy loop ───────────────────
# A port may not stock enough of a material for the barter goal. Rather than wait
# ~20 min for the timer, refresh the market immediately with a BLUE GEM (never red =
# real money). See vision/region_detectors/market_restock.py + memory
# project_unified_purchase_goal_design.

def refresh_market(capture_fn=None, tap_fn=None, *, ocr_fn=None, settle: float = 1.2,
                   verify_good: Optional[str] = None, port: Optional[str] = None,
                   read_market_fn=None, omni_fn=None) -> dict:
    """Restock the Purchase grid immediately by spending a BLUE gem on the refresh
    button. perceive→act→perceive: find the button, tap it, react to any confirm, and
    return — the caller re-perceives the restocked grid. Refuses anything not clearly
    blue-gem (red gem = real money; unknown colour is treated as unsafe).

    VERIFY: when the caller names `verify_good`, success is the GROUND-TRUTH signal —
    that good's Purchase tile going active again (stock>0, Sold-Out stamp gone). This
    is what the barter sub-task actually needs, and it's more reliable than the
    restock-timer OCR. Falls back to the timer reset when no good is named / unreadable."""
    if capture_fn is None:
        from capture.adb_capture import capture_screen as capture_fn
    if tap_fn is None:
        from actions.adb_actions import tap as tap_fn
    from vision.region_detectors.market_restock import find_restock_button

    btn = find_restock_button(capture_fn(), ocr_fn)
    if btn is None:
        return {"ok": False, "reason": "no restock control (market fresh or not on Purchase grid)"}
    if btn.currency != "blue_gem":
        return {"ok": False, "refused": True, "currency": btn.currency,
                "reason": f"restock cost is {btn.currency} (not a confirmed blue gem) — refused"}
    logger.info(f"[refresh] tap ↻ refresh icon @ ({btn.cx},{btn.cy}) (timer was {btn.timer})")
    tap_fn(btn.cx, btn.cy)
    time.sleep(settle)

    # Tapping ↻ opens the "Replenish Stock" dialog (Cancel | OK).  Its OK button is a plain
    # yellow button with NO cost+verb layout, so detect_commit_buttons MISSED it and tapped
    # the slider's gem+count instead (1199,631) — dismissing the dialog with NO refresh (live
    # 2026-08-18, user-caught).  Find OK by OCR: the 'OK'/'Confirm' token on the RIGHT half of
    # the dialog (Cancel sits on the left).
    from actions.sail_actions import _ocr_frame
    frame2 = capture_fn()
    accepted = False
    try:
        toks = [(w.strip().lower(), int(x), int(y))
                for w, c, x, y in _ocr_frame(frame2, min_conf=0.3)]
        okxy = next(((x, y) for w, x, y in toks
                     if w in ("ok", "confirm") and x > frame2.width * 0.45), None)
        if okxy:
            logger.info(f"[refresh] tap Replenish-Stock OK @ {okxy}")
            tap_fn(*okxy)
            time.sleep(settle)
            accepted = True
        else:
            logger.warning("[refresh] no OK button found on the refresh dialog")
    except Exception as exc:
        logger.debug(f"[refresh] OK-find failed: {exc}")

    # VERIFY by the ground truth ONLY — the sold-out good's TILE active again (stock>0), or
    # the restock timer RESETTING.  `accepted` (a tap happened) is NOT proof and must not
    # count as success: it let the broken refresh loop forever reporting ok=True.
    frame3 = capture_fn()
    tile_active = _good_in_stock(frame3, verify_good, port, read_market_fn, omni_fn) \
        if verify_good else None
    if tile_active is not None:
        ok, signal = tile_active, f"tile[{verify_good}] active={tile_active}"
    else:
        after = find_restock_button(frame3, ocr_fn)
        ok = _timer_went_up(btn.timer, after.timer if after else None)
        signal = f"timer {btn.timer}→{after.timer if after else '??'}"
    logger.info(f"[refresh] verify: accepted={accepted} {signal} → refreshed={ok}")
    return {"ok": ok, "currency": "blue_gem",
            "reason": f"refresh {'ok' if ok else 'NOT confirmed'} ({signal})"}


def _good_in_stock(frame, good: str, port=None, read_market_fn=None, omni_fn=None):
    """True if `good`'s Purchase tile is active (stock>0, not sold out), False if still
    sold out, None if the tile can't be read. The ground-truth refresh signal."""
    if not good:
        return None
    if read_market_fn is None:
        from vision.market_reader import read_market_page_omni as read_market_fn
    if omni_fn is None:
        from vision.omniparser import parse_fast_cached as omni_fn
    try:
        goods = read_market_fn(frame, tab="purchase", port=port, elements=omni_fn(frame)) or []
    except Exception as exc:
        logger.debug(f"[refresh] tile-verify read failed: {exc}")
        return None
    g = next((x for x in goods if (_name(x) or "").lower() == good.lower()), None)
    if g is None:
        return None
    return (getattr(g, "available_qty", 0) or 0) > 0 and not getattr(g, "sold_out", False)


def _timer_went_up(before: str, after: str) -> bool:
    """True if the restock timer jumped UP (reset to a fresh cycle) — the deterministic
    signal that a refresh took effect (vs just ticking down)."""
    def _secs(t):
        if not t:
            return None
        try:
            parts = [int(p) for p in t.replace(".", ":").split(":")]
        except ValueError:
            return None
        return sum(p * 60 ** i for i, p in enumerate(reversed(parts)))
    a, b = _secs(after), _secs(before)
    return a is not None and b is not None and a > b + 30


def _on_sell_tab(frame) -> bool:
    """True when the market's SELL tab is the active one.

    The Purchase and Sell grids look alike but mean opposite things: Purchase lists the
    SHOP'S stock, Sell lists what the FLEET holds. Confusing them makes "do I already own
    enough?" answer with the shop's inventory.

    Anchored on the COMMIT BUTTON, which reads "Sell" on the sell grid and "Purchase" on the
    buy grid from the same slot. Both tab titles are always painted in the left sub-menu, so
    a text-order heuristic cannot separate them — the previous one answered True on a
    Purchase grid and False on a genuine Sell grid (live 2026-08-22, which cost the mission
    its hold reading and left it planning zero rounds).
    """
    # FIRST ANCHOR: the chromed title. In this game the title IS the sub-menu item currently
    # selected and highlighted (user 2026-08-22: "if Purchase is highlighted, the title is
    # Purchase, when Supply is highlighted, the title is Supply"), so it answers "am I on the
    # Sell sub-menu?" directly, with no tap and no inference.
    try:
        from actions.ui import on_submenu
        if on_submenu("sell", frame):
            return True
    except Exception as exc:
        logger.debug(f"[buy_to_goal] title read failed: {exc}")
    # SECOND ANCHOR: the commit button, which reads "Sell" on this grid and "Purchase" on the
    # buy grid from the same slot.
    try:
        from vision.omniparser import parse_fast_cached
        from vision.region_detectors.action_buttons import detect_action_buttons
        els = parse_fast_cached(frame)
        region = detect_action_buttons(els, frame.width, frame.height)
        labels = [str(l).lower() for l in (region.labels() if region else [])]
    except Exception as exc:
        logger.debug(f"[buy_to_goal] sell-tab check failed: {exc}")
        return False
    if any("purchase" in l for l in labels):
        return False                     # the buy grid's commit button — definitely not Sell
    return any("sell" in l for l in labels)


def _read_owned_via_sell(capture_fn, tap_fn, settle: float = 1.2) -> dict:
    """Owned units per good, read from the SELL tab (its count overlay is OCR'd reliably by
    read_market_page_omni, unlike the Purchase-side HOG panel).  Switches to the Sell tab, reads,
    returns {name_lower: qty}.  Empty dict on any failure (caller then just buys)."""
    from actions.market_actions import MARKET_COORDS
    from vision.market_reader import read_market_page_omni
    try:
        # Find the Sell tab; the calibrated point is only a fallback. Then VERIFY we are on
        # it before trusting anything read.
        frame = capture_fn()
        # Switch tabs only if we are not already there. Tapping "Sell" while the Sell page is
        # OPEN is not a no-op: the page TITLE is also the word "Sell", sitting beside the back
        # arrow, and that is what the label search matched — live 2026-08-22 the tap landed at
        # (107,53) on the header of a Sell page showing Ebony 700 / Coral 797, navigated away,
        # and the verification then correctly reported "not the Sell tab".
        if not _on_sell_tab(frame):
            from actions.sail_actions import _find_button
            pos = _find_button(frame, "sell") or MARKET_COORDS["sell"]
            tap_fn(*pos)
            time.sleep(settle)
            frame = capture_fn()
        if not _on_sell_tab(frame):
            # Reading the PURCHASE grid here returns the SHOP'S stock as if it were ours.
            # Live 2026-08-22 at Kolkata that answered "textiles: 3" while the hold carried
            # 920, so the pre-check decided it had to buy and loaded Textiles one at a time.
            # Unknown must stay unknown.
            logger.warning("[buy_to_goal] could not confirm the Sell tab — refusing to "
                           "report owned counts (a Purchase-grid read would be shop stock)")
            return {}
        rows = read_market_page_omni(frame, tab="sell")
    except Exception as exc:
        logger.debug(f"[buy_to_goal] sell owned-read failed: {exc}")
        return {}
    owned = {(g.name or "").lower(): g.owned_qty
             for g in rows if getattr(g, "owned_qty", None)}
    if owned:
        # Stamp it. The hold is only READABLE at a market, but it is just as true at sea —
        # live 2026-08-22 a mission with 700 Ebony / 797 Coral / 1049 Textiles aboard planned
        # ZERO rounds because it was on the water and could not open a Sell tab to look.
        from memory.observed_facts import remember
        remember("hold", owned)
    return owned


def buy_to_goal(port: str, goal: Mapping[str, int], *, max_rounds: int = 6,
                capture_fn=None, tap_fn=None, omni_fn=None, read_market_fn=None,
                buy_round_fn=None, refresh_fn=None, cargo_fn=None,
                clear_blockers_fn=None, show_grid_fn=None,
                tile_grayed_fn=None, find_tile_fn=None, read_owned_fn=None,
                settle: float = 1.2) -> dict:
    """Buy toward a total material target. Bounded perceive→act→perceive loop (each round
    does ONE thing — buy an available shelf, or refresh a sold-out one), NOT a blind
    subloop. `max_rounds` caps total rounds.

    Progress is measured by the RELIABLE cargo-total delta, not the per-good
    available_qty (the latter under-read live 2026-08-17). Each round:
      • good AVAILABLE  → buy it; if the goal isn't met and stock remains, buy again next
        round. (Live 2026-08-18: at Malé, Coral does NOT sell out — it stays available at
        a rising price — so buying repeatedly, not refreshing, is what reaches the goal.)
      • good SOLD OUT   → blue-gem refresh to restock, then buy next round.
      • good gone / no progress (cargo full) → stop.

    An EMPTY grid read is a PERCEIVE failure (a promo/announcement popup over the grid, or
    the greeting landing page), NOT "nothing to buy" — clear blockers + re-open the
    Purchase grid and retry before concluding (live 2026-08-18: a "Shortcut to Growth"
    promo covered the grid → bought 0 with no retry). Returns {ok, met, bought_total,
    goal_total, rounds}.
    """
    if capture_fn is None:
        from capture.adb_capture import capture_screen as capture_fn
    if tap_fn is None:
        from actions.adb_actions import tap as tap_fn
    if omni_fn is None:
        from vision.omniparser import parse_fast_cached as omni_fn
    if read_market_fn is None:
        from vision.market_reader import read_market_page_omni as read_market_fn
    if buy_round_fn is None:
        buy_round_fn = purchase_goods
    if refresh_fn is None:
        refresh_fn = refresh_market
    # Progress tracking: the DEFAULT (real) path IDENTIFIES the good's cargo tile by which
    # right-panel number ROSE after a buy (we know what we bought → no icon guessing), then
    # follows it — see track_bought_good.  Reads the ACTUAL owned count → goal means "OWN N".
    # Tests inject `cargo_fn(frame)->int` (a total-cargo stand-in) and take that path instead.
    _track = cargo_fn is None
    if read_owned_fn is None:
        read_owned_fn = _read_owned_via_sell
    if cargo_fn is None:
        cargo_fn = lambda _fr: None      # unused on the tracker path
    if tile_grayed_fn is None:
        tile_grayed_fn = _tile_grayed
    if find_tile_fn is None:
        find_tile_fn = _find_material_tile
    if clear_blockers_fn is None:
        from brain.unexpected_dialog import clear_blockers as clear_blockers_fn
    if show_grid_fn is None:
        from actions.market_actions import MARKET_COORDS as _MC
        def show_grid_fn():
            # Open the Purchase grid by the DETECTED left-menu item. The greeting page
            # ("Market Owner" landing) places "Purchase" lower than grid-mode, so the
            # calibrated coord (70,145) misses it (live 2026-08-18). Fall back to it.
            frame = capture_fn()
            xy = _find_purchase_menu(omni_fn(frame))
            tap_fn(*(xy or _MC["purchase"]))
            time.sleep(settle)

    def _read(frame):
        els = omni_fn(frame)
        goods = read_market_fn(frame, tab="purchase", port=port, elements=els) or []
        return {(_name(g) or "").lower(): g for g in goods}, els

    def _read_grid():
        """Perceive the goods grid. An empty read = perceive failure (blocker over the
        grid / greeting page), so clear blockers, re-show the grid, and retry — not
        'nothing to buy'. Returns (frame, {name: good}, elements, cleared_any)."""
        cleared_any = False
        frame = capture_fn()
        for _ in range(3):
            goods, els = _read(frame)
            if goods:
                return frame, goods, els, cleared_any
            try:
                if clear_blockers_fn(frame).get("cleared"):
                    cleared_any = True
            except Exception as exc:
                logger.debug(f"[buy_to_goal] clear_blockers failed: {exc}")
            show_grid_fn()                       # (re)open the Purchase grid
            frame = capture_fn()
        goods, els = _read(frame)
        return frame, goods, els, cleared_any

    def _do_refresh(attempt, verify_good):
        """Blue-gem refresh to restock a sold-out shelf; append to rounds. Returns ok."""
        r = refresh_fn(capture_fn=capture_fn, tap_fn=tap_fn, verify_good=verify_good, port=port)
        rounds.append({"attempt": attempt, "refresh": r})
        return bool(r.get("ok"))

    goal_total = sum(goal.values())
    bought_total = 0              # best-known OWNED count of the good (from the tracked tile)
    tracked_pos = None            # remembered cargo tile of the good being bought
    refreshed_last = False        # did the previous round end in a refresh? (cargo-full detection)
    rounds: list = []

    # COLD pre-check via the SELL tab (no buying): how many do we ALREADY own?  Read from the Sell
    # tab's count overlay — reliable, unlike the Purchase-side HOG panel which mis-OCRs multi-digit
    # counts (live 2026-08-19: real 1,444 Textiles read as 14444/444).  If the ship already holds
    # the goal (PER GOOD, so a surplus of one material can't mask a shortfall of another), skip
    # buying / burning gems.  Then re-open the Purchase grid for the buy loop.
    if _track:
        owned = read_owned_fn(capture_fn, tap_fn, settle) or {}
        pre_owned = sum(owned.get(m.lower(), 0) for m in goal)
        if pre_owned:
            bought_total = pre_owned
            rounds.append({"pre_owned": pre_owned, "owned": owned})
            if all(owned.get(m.lower(), 0) >= q for m, q in goal.items()):
                return {"ok": True, "met": True, "bought_total": bought_total,
                        "goal": dict(goal), "goal_total": goal_total, "rounds": rounds,
                        "reason": f"already own {owned} ≥ goal"}
        show_grid_fn()                       # pre-check left us on the Sell tab → back to Purchase

    for attempt in range(max_rounds):
        frame, goods, els, cleared = _read_grid()
        if cleared:
            rounds.append({"attempt": attempt, "cleared_blocker": True})
        # Buyable = on the grid AND not stamped Sold Out. We do NOT gate on available_qty
        # (the reader parses that stock badge intermittently — 173 vs None live 2026-08-18).
        buyable = [m for m in goal
                   if goods.get(m.lower()) is not None
                   and not getattr(goods[m.lower()], "sold_out", False)]
        sold_out_needed = any(getattr(goods.get(m.lower()), "sold_out", False) for m in goal)

        if buyable:
            snap_before = _cargo_tiles(frame, els) if _track else None
            cargo_before = None if _track else cargo_fn(frame)
            tiles = {m: find_tile_fn(els, m) for m in buyable}   # tile positions (before-buy)
            res = buy_round_fn(port, goods=list(buyable), capture_fn=capture_fn,
                               tap_fn=tap_fn, omni_fn=omni_fn, settle=settle)
            time.sleep(settle)                    # let the buy settle so the cargo counter updates
            # Re-perceive via the ROBUST path — the buy leaves a result/negotiation dialog
            # or drops to the greeting page (live 2026-08-18).
            after, goods_after, els_after, _ = _read_grid()
            if _track:
                # OWNED count of the bought good = the cargo tile whose number rose (the tile
                # we just bought), matched by count so panel reflow doesn't fool it.
                owned, tracked_pos = track_bought_good(snap_before, _cargo_tiles(after, els_after),
                                                       tracked_pos, last_owned=bought_total or None)
                cargo_after = owned
                got = (owned - bought_total) if owned is not None else None   # this round's rise
            else:
                cargo_after = cargo_fn(after)
                got = (cargo_after - cargo_before) if (cargo_before is not None
                                                       and cargo_after is not None) else None
            # Progress = the OWNED count itself — "own N", stop when the ship holds enough.
            # max() so a stale/failed read never DECREASES the running total.
            if cargo_after is not None:
                bought_total = max(bought_total, cargo_after)
            # SOLD-OUT SIGNAL (user 2026-08-18): a bought good's tile GRAYED OUT across the
            # buy — a coarse saturation drop at the tile position, cheaper + more robust
            # than detecting the "Sold Out" stamp. `got == 0` (buy no-op'd) backstops the
            # already-empty case where there's no colour transition to see.
            emptied = [m for m in buyable
                       if tiles.get(m) and tile_grayed_fn(frame, after, tiles[m])]
            rounds.append({"attempt": attempt, "buyable": buyable, "cargo_delta": got,
                           "emptied": emptied, "buy_ok": bool(res.get("ok"))})
            # Log the progress the stop condition depends on. Without this the loop's own
            # blindness is invisible: on 2026-08-21 the tracker returned None every round,
            # bought_total never moved off 0, and the run bought 1,681 Ebony against a goal
            # of 350 with nothing in the log to show why it would not stop.
            logger.info(f"[buy_to_goal] round {attempt + 1}/{max_rounds}: owned="
                        f"{cargo_after if cargo_after is not None else 'UNREADABLE'} "
                        f"(+{got if got is not None else '?'}) → {bought_total}/{goal_total}")
            if bought_total >= goal_total:
                logger.info(f"[buy_to_goal] goal met — {bought_total} ≥ {goal_total}")
                break
            made_progress = got is not None and got > 0
            if emptied:
                # shelf SOLD OUT (tile grayed) → restock with a blue-gem refresh (if a round remains),
                # regardless of whether we bought some this round.  Only count it toward "cargo full"
                # if this round made NO progress.
                if attempt >= max_rounds - 1 or not _do_refresh(attempt, emptied[0]):
                    break
                refreshed_last = not made_progress
            elif made_progress:
                refreshed_last = False              # bought some, shelf active → keep buying
            elif got == 0:
                if refreshed_last:
                    # We refreshed LAST round (that refresh confirmed the shelf is restocked/active)
                    # yet STILL bought 0 → the shelf HAS stock but the ship can't LOAD it → the cargo
                    # hold is full (or the free room is reserved for supplies via Apply Load Ratio —
                    # the N/M counter can't tell, user 2026-08-19).  STOP; more refreshes just burn
                    # blue gems (live: 4 wasted at Jakarta).  Caller must SELL surplus to free space.
                    # [Precise signal to wire: at load time, no new active cart tile + Purchase
                    # button disabled.]
                    logger.info("[buy_to_goal] still 0 after a refresh → can't load (cargo full) — "
                                "stopping; sell surplus to free space")
                    rounds.append({"attempt": attempt, "cargo_full": True})
                    break
                # Maybe the reader just missed a sold-out shelf → refresh ONCE and retry next round.
                if attempt >= max_rounds - 1 or not _do_refresh(attempt, buyable[0]):
                    break
                refreshed_last = True
            continue

        # Nothing buyable per the reader's Sold-Out flag → refresh a flagged good, else stop.
        if not sold_out_needed or attempt >= max_rounds - 1:
            break
        vg = next((m for m in goal if getattr(goods.get(m.lower()), "sold_out", False)), None)
        if not _do_refresh(attempt, vg):
            break

    met = bought_total >= goal_total
    return {"ok": met or bought_total > 0, "met": met, "bought_total": bought_total,
            "goal": dict(goal), "goal_total": goal_total, "rounds": rounds,
            "reason": f"bought ~{bought_total}/{goal_total}"
                      + ("" if met else f" (stopped after {len(rounds)} rounds / {max_rounds} max)")}


def _hog_sim(a, b) -> float:
    """Cosine similarity of HOG (histogram-of-oriented-gradients) descriptors — matches on icon
    SHAPE via the distribution of edge ORIENTATIONS, robust to the brightness / background /
    grayed-out / scale differences between the coloured Purchase-grid thumbnail and the cargo-
    panel icon.  Grayscale AND plain gradient-magnitude correlation both fail on the grayed
    cargo icons (ranked a gem over the fabric); HOG picks the right tile with a clear margin
    (benchmarked 0.83-0.88 vs ~0.6 runner-up, 2026-08-19)."""
    import numpy as np
    from skimage.feature import hog
    def _v(im):
        g = np.asarray(im.convert("L").resize((64, 64)), dtype=np.float32)
        v = hog(g, orientations=8, pixels_per_cell=(16, 16), cells_per_block=(1, 1))
        return v - v.mean()
    va, vb = _v(a), _v(b)
    d = (np.linalg.norm(va) * np.linalg.norm(vb)) or 1.0
    return float(va @ vb / d)


def _icon_top(im, frac: float = 0.62):
    """Keep the TOP `frac` of an icon crop — drops the count / "0" badge strip at the bottom
    (the digit edges are identical across tiles and dilute the shape match; user 2026-08-18)."""
    w, h = im.size
    return im.crop((0, 0, w, max(1, int(h * frac))))


_SUPPLY_REFS = None


def _supply_refs():
    """Cached _icon_top'd reference crops for the FOOD + WATER supply icons.  Supplies are always
    in cargo (when carried), aren't trade goods, and their rounded shapes are HOG-close to many
    goods (bread↔turmeric/masala/…), so they're collision magnets — a trade-good query must never
    match them.  Kept as saved PNGs in vision/assets (icons are constant across ports/frames)."""
    global _SUPPLY_REFS
    if _SUPPLY_REFS is None:
        from pathlib import Path
        from PIL import Image
        base = Path(__file__).resolve().parents[1] / "vision" / "assets"
        _SUPPLY_REFS = []
        for name in ("cargo_food.png", "cargo_water.png"):
            p = base / name
            if p.exists():
                _SUPPLY_REFS.append(_icon_top(Image.open(p).convert("RGB")))
    return _SUPPLY_REFS


def _is_supply_tile(crop, thresh: float = 0.85) -> bool:
    """True if this cargo-tile icon matches the food or water reference (HOG ≥ thresh).  Supply
    tiles self-match ≥0.95; trade goods top out ~0.78 — so 0.85 separates them.  Absent supplies
    simply don't match (the ship may carry neither), so nothing is wrongly excluded."""
    return any(_hog_sim(r, crop) >= thresh for r in _supply_refs())


# The cargo strip's tiles are evenly pitched (~137px apart, measured live 2026-08-21).
# The market's goods grid also carries numbers, but it sits far to the left with a much
# bigger gap before the strip begins (grid at x≈450/874/1312, then 376px of nothing, then
# the strip at 1688/1825/1964/2101). So the strip is the RIGHTMOST run of evenly-spaced
# numbers — a relative property that survives the camera-cutout shift.
_CARGO_TILE_MAX_GAP = 220        # > one tile pitch, << the gap to the goods grid

# THE RIGHT PANEL IS BOTH CART AND CARGO (user 2026-08-22):
#   * ACTIVE  (full brightness) = staged IN THE CART — not yours yet
#   * GRAYED  (dimmed)          = already in CARGO — owned
# A staged tile also carries a red ✕ that removes it and returns the goods to the list.
# Once a purchase completes the cart tile disappears and its amount joins the owned tile.
#
# Tapping an ACTIVE tile in the right panel un-stages it, and how depends on Put In Bulk:
#   * bulk CHECKED   → the whole stack is removed from the cart at once
#   * bulk UNCHECKED → a quantity dialog opens, exactly like tapping a tile in the left
#                      grid, except the action button reads **Remove** instead of Load,
#                      and the number chosen is how many to take OUT of the cart.
# That is the clean way to undo an accidental stage. On 2026-08-22 the bot instead pressed
# Back three times to escape a cart it had filled by mistake.
#
# The graying is the primary signal: it is the tile's STATE, whereas the ✕ is just a control
# that happens to sit on staged tiles. Measured on frame_0037 of the 2026-08-21 Jakarta run,
# sampling the tile body (excluding the badge corner and the count strip):
#
#     Ebony 140  CART   brightness 141      <- same good, twice as bright
#     Ebony 421  owned  brightness  70
#     Textiles   owned  brightness  87
#     Coral      owned  brightness  49
#     Water      owned  brightness  42
#
# The same owned Ebony tile reads 70 on both frame_0030 (count 281) and frame_0037 (count
# 421), so brightness tracks the state, not the good or the quantity.
_CART_TILE_MIN_BRIGHTNESS = 110   # owned measured <= 87, cart 141
_CART_BADGE_MIN_RED_PX = 60       # corroborating signal: 541 on the cart tile, 0 on owned


def _tile_in_cart(frame, e) -> bool:
    """True when this cargo tile is staged in the cart rather than owned (purchased).

    The distinction is load-bearing for the "own N" stop condition. The panel shows the SAME
    good twice while a purchase is pending — e.g. Ebony 140 (cart, bright + red ✕) beside
    Ebony 421 (owned, muted) — so counting the cart tile as owned reads the wrong number,
    and on 2026-08-21 it made the tracker follow the cart tile and report a count that went
    BACKWARDS (421 → 140).
    """
    if frame is None:
        return False
    try:
        import numpy as np
        a = np.asarray(frame.convert("RGB")).astype(int)
        h, w = a.shape[:2]
        x1, y1 = max(int(e.x1), 0), max(int(e.y1), 0)
        x2, y2 = min(int(e.x2), w), min(int(e.y2), h)
        if x2 - x1 < 40 or y2 - y1 < 40:
            return False

        # NOT GRAYED = staged. Sample the tile body, skipping the badge corner on the right
        # and the count strip along the bottom, both of which are drawn bright either way.
        body = a[y1 + 10:y2 - 30, x1 + 8:x2 - 45]
        if body.size:
            brightness = float(body.max(axis=2).mean())
            if brightness >= _CART_TILE_MIN_BRIGHTNESS:
                return True

        # Corroborating: the red ✕ remove control only appears on staged tiles.
        corner = a[y1:min(y1 + 38, h), max(x2 - 42, 0):x2]
        if corner.size:
            R, G, B = corner[:, :, 0], corner[:, :, 1], corner[:, :, 2]
            if int(((R > 130) & (R - G > 55) & (R - B > 55)).sum()) >= _CART_BADGE_MIN_RED_PX:
                return True
        return False
    except Exception as exc:
        logger.debug(f"[cargo] cart/owned check skipped: {exc}")
        return False


def _cargo_tiles(frame, elements=None):
    """Every cargo tile in the market's RIGHT panel as (center, count).  OmniParser labels
    these tiles with their number, so no OCR needed.

    The tiles are found as the rightmost evenly-pitched RUN, not by an absolute x cutoff.

    WHY THAT MATTERS. The strip fills RIGHT-TO-LEFT, and the good currently being bought
    always takes the leftmost owned slot. Which PIXEL that is depends on how many distinct
    goods are in the hold — so an absolute cutoff does not fail consistently, it fails only
    once the hold is full enough, which is the worst way to fail. Measured 2026-08-21:

        Malé,    2 owned goods (Coral, water)               → Coral   at x≈1960  visible
        Jakarta, 4 owned goods (Ebony, Coral, Textiles, water) → Ebony at x≈1680  HIDDEN

    The old filter required x1 > 1780, so it tracked Coral perfectly (56 → 170 → 284 → 455)
    and was blind to Ebony (1 → 141 → 281 → 421). Coral was not handled correctly; it was
    handled LUCKILY — the cutoff happened to sit between the 3-slot and 4-slot layouts. Any
    gather at a port where the hold already held 3+ distinct goods would have failed the
    same way. Jakarta reached four because Ebony was ALREADY in the hold (count 1) before
    the first purchase there, on top of the Coral and Textiles gathered earlier.

    With the tracked tile invisible, `track_bought_good` saw identical before/after multisets
    every round, returned None, `bought_total` never rose, and the "own N" stop condition
    could not fire: the goal was 350 Ebony and the bot bought 1,681 (3.56M ducats, hold at
    93%) before being stopped by hand.
    """
    import re
    if elements is None:
        from vision.omniparser import parse_fast_cached
        elements = parse_fast_cached(frame)

    candidates = []
    for e in elements:
        if getattr(e, "element_type", "") != "button":
            continue
        if not (240 < getattr(e, "y1", 0) < 560):
            continue
        lab = (getattr(e, "label", "") or "").strip()
        if re.fullmatch(r"[\d,]+", lab or "x"):
            n = int(re.sub(r"[^\d]", "", lab))
            if 0 < n <= 5000:            # reject OCR misreads (14444) — cargo cap is ~4,108
                candidates.append((e, n))

    if not candidates:
        return []

    # Walk leftward from the rightmost number, taking tiles while the gap stays within one
    # pitch. The first big gap is the boundary between the cargo strip and the goods grid.
    candidates.sort(key=lambda c: c[0].x1, reverse=True)
    run = [candidates[0]]
    for e, n in candidates[1:]:
        if run[-1][0].x1 - e.x1 <= _CARGO_TILE_MAX_GAP:
            run.append((e, n))
        else:
            break

    # Drop cart-staged tiles AFTER the run is built: they sit inside the strip, so removing
    # them earlier would break the pitch continuity the run detection relies on.
    owned = [(e, n) for e, n in run if not _tile_in_cart(frame, e)]
    dropped = len(run) - len(owned)
    if dropped:
        logger.debug(f"[cargo] ignoring {dropped} cart-staged tile(s) — not owned yet")
    return [(((e.x1 + e.x2) // 2, (e.y1 + e.y2) // 2), n) for e, n in owned]


def _nearest_tile(pos, tiles, max_d=90):
    """The (center, count) tile nearest `pos` within max_d px, or None."""
    if not tiles:
        return None
    t = min(tiles, key=lambda e: (e[0][0] - pos[0]) ** 2 + (e[0][1] - pos[1]) ** 2)
    return t if (t[0][0] - pos[0]) ** 2 + (t[0][1] - pos[1]) ** 2 <= max_d ** 2 else None


def track_bought_good(before, after, tracked, last_owned=None):
    """Identify the just-bought good's cargo tile as the one whose number ROSE across the
    buy (we KNOW which good we bought, so the tile that increased is it — no icon match).
    Once known, follow it by position.  Returns (owned_count | None, tile_pos | None).

    Matched by COUNT, not position: buying reflows the panel (a NEW good adds a tile and
    pushes others; selling removes one), so before/after positions don't line up.  Every
    UNCHANGED good has the same count in before and after — cancel those out (multiset), and
    the one after-tile left over is the good we just bought (its count changed, or it's the
    new tile).  Position-independent, so it survives the reflow (user 2026-08-18 edge cases).
    Returns (owned_count | None, tile_pos | None); None owned = buy didn't register.
    """
    from collections import Counter
    bc = Counter(n for _, n in before)
    leftover = []
    for pos, num in after:
        if bc[num] > 0:
            bc[num] -= 1                      # an unchanged good — cancel it
        else:
            leftover.append((pos, num))       # count not seen before → the bought good
    if len(leftover) == 1:
        return leftover[0][1], leftover[0][0]
    if leftover:                              # ambiguous — several tiles changed at once
        # While BUYING, the tracked good's count only ever GROWS. Prefer the leftover that
        # continues that growth: the smallest value at or above what we last owned.
        #
        # Position is the wrong tie-breaker here, because the strip REFLOWS — a newly
        # acquired good is inserted at the left and pushes the others right. Live
        # 2026-08-21: Ebony went 281 → 421 and moved from x≈1688 to x≈1825 while a new
        # tile (140) took the slot Ebony had just left. Nearest-to-tracked therefore
        # picked 140 — a different good — and the owned count went BACKWARDS.
        if last_owned is not None:
            growing = [t for t in leftover if t[1] >= last_owned]
            if growing:
                pick = min(growing, key=lambda t: t[1])
                return pick[1], pick[0]
        if tracked is not None:
            near = min(leftover, key=lambda t: (t[0][0] - tracked[0]) ** 2 + (t[0][1] - tracked[1]) ** 2)
            return near[1], near[0]
        best = max(leftover, key=lambda t: t[1])
        return best[1], best[0]
    if tracked is not None:                   # nothing changed → buy didn't register
        t = _nearest_tile(tracked, after)
        if t is not None:
            return t[1], t[0]
    return None, tracked


def _grid_thumb(frame, grid_el, elements=None):
    """Icon graphic for a Purchase-grid row: its LEFTMOST SQUARE ≈ ⅓ of the row width.
    `min(height, 0.35·width)` crops JUST the icon — excludes the name text (right) and price bar
    (below) — across both row layouts OmniParser emits: the TALL row-with-price-bar (~430×242 →
    side≈150) and the SHORT icon+name strip (~430×150 → height bounds it to 150).  No dependence
    on the `icon` element (absent on short rows; a wide name-spanning strip when present).

    NB: this deliberately leaves a small name-text sliver rather than tightly cropping the icon
    square.  Detecting the exact square (the icon sits on an obvious square bg) IS feasible for
    ~7/8 goods, but a tight crop shifts the HOG framing so it no longer matches the CARGO tiles —
    which are themselves full squares with icon-background margin, not tight icons — and that
    reintroduced a false positive (Masala→281).  The sliver keeps query/cargo framing consistent
    (user asked 2026-08-19; measured, reverted).  Number strip cut by _icon_top."""
    w, h = grid_el.x2 - grid_el.x1, grid_el.y2 - grid_el.y1
    side = min(h, int(0.35 * w))
    x0, y0 = grid_el.x1 + 2, grid_el.y1 + 4              # ~3px left of the old icon-based crop
    return _icon_top(frame.crop((x0, y0, x0 + side, y0 + side)))


def read_cargo_good(frame, good_name, elements=None, min_score=0.70, min_margin=0.10):
    """How many of `good_name` the ship holds, from the market's RIGHT cargo panel — a COLD
    read (no buying needed): match this port's NAMED Purchase-grid thumbnail to the iconic
    (nameless) cargo tiles by HOG shape similarity and read the number at the best-matching
    tile.  HOG discriminates the icons where grayscale / edge correlation could not (2026-08-19);
    it reliably picks the right tile across frames with a clear margin.

    Guards the "we own NONE of this good" case so it never blindly returns a tile:
      • SUPPLY EXCLUSION — food/water tiles are dropped from the candidate pool first (not trade
        goods, and the biggest HOG collision magnets — see _is_supply_tile);
      • min_score — the best match must be strong (an absent good's best tops out below a present
        good's ~0.85 self-match);
      • min_margin — the best must beat the runner-up (ambiguous → not present);
      • UNIQUENESS — the winning tile must belong to THIS good: no OTHER grid good matches that
        tile better.  Two goods can have HOG-similar icons (Turmeric vs the Textiles fabric tile
        collided live) — the tile goes to whichever good matches it best, the other reads None.
    (No colour test: the cargo panel amber-tints every icon, so a vivid blue Sapphire grid icon
    vs its amber-grayed cargo tile disagree in hue though the match is CORRECT — colour is not a
    valid discriminator here.)

    Returns (count, (cx, cy)) or None.  Needs the good on THIS port's grid (the naming reference).
    """
    import re
    if elements is None:
        from vision.omniparser import parse_fast_cached
        elements = parse_fast_cached(frame)
    key = good_name.lower()
    cargo = [e for e in elements
             if getattr(e, "element_type", "") == "button" and getattr(e, "x1", 0) > 1780
             and 240 < getattr(e, "y1", 0) < 560
             and re.fullmatch(r"[\d,]+", (getattr(e, "label", "") or "").strip() or "x")]
    if not cargo:
        return None

    def _center(e):
        return ((e.x1 + e.x2) // 2, (e.y1 + e.y2) // 2)

    def _count(e):
        try:
            return int(re.sub(r"[^\d]", "", e.label))
        except ValueError:
            return None

    grids = [e for e in elements
             if getattr(e, "element_type", "") == "button" and getattr(e, "x1", 9999) < 1600
             and (getattr(e, "label", "") or "").strip()]
    grid = next((e for e in grids if key in (e.label or "").lower()), None)
    if grid is None:
        return None

    cargo_crops = [_icon_top(frame.crop((e.x1 + 2, e.y1 + 2, e.x2 - 2, e.y2 - 2))) for e in cargo]
    # Drop FOOD/WATER supply tiles from the candidate pool — they're not trade goods and their
    # rounded shapes are the biggest HOG collision magnets (5/7 absent goods matched bread/bucket).
    # Position-independent + safe when supplies aren't carried: only tiles matching the ref drop.
    keep = [i for i, c in enumerate(cargo_crops) if not _is_supply_tile(c)]
    cargo = [cargo[i] for i in keep]
    cargo_crops = [cargo_crops[i] for i in keep]
    if not cargo:
        return None
    thumb = _grid_thumb(frame, grid, elements)
    scored = sorted(((_hog_sim(thumb, c), i) for i, c in enumerate(cargo_crops)), key=lambda t: -t[0])
    best_s, bi = scored[0]
    margin = best_s - (scored[1][0] if len(scored) > 1 else 0.0)
    if best_s < min_score or margin < min_margin:
        return None                          # too weak / ambiguous → treat as not-present

    # UNIQUENESS: does any OTHER grid good match the winning tile better than this one?  If so
    # the tile is that good's, and we own NONE of this one (don't return the wrong count).
    for g in grids:
        if g is grid or key in (g.label or "").lower():
            continue
        if _hog_sim(_grid_thumb(frame, g, elements), cargo_crops[bi]) > best_s:
            return None

    c = _count(cargo[bi])
    if c is None:
        return None
    return c, _center(cargo[bi])


def _read_cargo_used_cap(frame):
    """(used, capacity) from the 'N/M' cargo-load counter in the cart panel (RIGHT side), or None.
    Reliable OCR, unlike per-good available_qty."""
    import re
    from actions.sail_actions import _ocr_frame
    best = None
    for w, _c, x, y in _ocr_frame(frame, min_conf=0.3):
        m = re.search(r"([\d,]{2,})\s*/\s*([\d,]{2,})", w)
        if m and x > 1850:                       # cart panel, not Trade Points (left)
            try:
                cur, cap = int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))
            except ValueError:
                continue
            if cap >= 500 and (best is None or cap > best[1]):   # largest cap = cargo hold
                best = (cur, cap)
    return best


def _read_cargo_total(frame) -> Optional[int]:
    """Current cargo units from the 'N/M' load counter (the N). None if unreadable."""
    uc = _read_cargo_used_cap(frame)
    return uc[0] if uc else None


def _name(g):
    return getattr(g, "name", None) or getattr(g, "label", None)


def _tile_grayed(before, after, xy, *, half_w: int = 200, half_h: int = 110,
                 thresh: float = 25.0) -> bool:
    """Coarse SOLD-OUT signal (user 2026-08-18): a good's tile DARKENS markedly when it
    sells out. Coarsely compare the tile region (a crop centred on the tile position)
    between the before- and after-buy frames — a large mean pixel change = the tile grayed
    out. Cheap, and no "Sold Out" stamp / OCR detection needed. Live calibration
    (rf2): a full-shelf buy dimmed the tile brightness 182→91, meanAbsDiff≈84; a no-op
    left it pixel-identical (diff 0, handled by the got==0 backstop)."""
    if not xy or before is None or after is None:
        return False
    try:
        import numpy as np
        x, y = int(xy[0]), int(xy[1])
        box = (max(0, x - half_w), max(0, y - half_h), x + half_w, y + half_h)
        rb = np.asarray(before.convert("RGB").crop(box), dtype=np.float32)
        ra = np.asarray(after.convert("RGB").crop(box), dtype=np.float32)
        if rb.size == 0 or rb.shape != ra.shape:
            return False
        return float(np.abs(rb - ra).mean()) >= thresh
    except Exception:
        return False


def _find_purchase_menu(elements):
    """Detected (cx,cy) of the left-menu 'Purchase' item, or None. The greeting page
    lays it out differently than grid-mode, so we locate it rather than hardcode a coord
    (live 2026-08-18: the calibrated (70,145) hit the title bar on the greeting page)."""
    for e in elements or []:
        lab = (_name(e) or "").strip().lower()
        cx = getattr(e, "cx", None)
        if "purchase" in lab and cx is not None and cx < 400:
            return (int(cx), int(getattr(e, "cy", 0)))
    return None
