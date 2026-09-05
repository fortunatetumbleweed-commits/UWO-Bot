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

import re
import time
from typing import Callable, Mapping, Optional

from loguru import logger


def _tile_is_greyed(frame, tile) -> bool:
    """Is the goods tile at (cx, cy) greyed out — i.e. sold out?

    Reuses the market reader's single-frame signal (low saturation, low brightness over the
    tile's artwork) rather than inventing a second notion of grey. Unknown counts as NOT
    greyed: a check that cannot see must not be the thing that stops a buy.
    """
    try:
        import numpy as np
        cx, cy = int(tile[0]), int(tile[1])
        art = np.asarray(frame.convert("RGB")).astype(float)[
            max(cy - 46, 0):cy + 46, max(cx - 53, 0):cx + 53]
        if art.size == 0:
            return False
        mx, mn = art.max(axis=2), art.min(axis=2)
        sat = float(((mx - mn) / np.maximum(mx, 1)).mean())
        from vision.market_reader import _SOLD_OUT_MAX_SAT, _SOLD_OUT_MAX_BRIGHT
        return sat <= _SOLD_OUT_MAX_SAT and float(art.mean()) <= _SOLD_OUT_MAX_BRIGHT
    except Exception as exc:
        logger.debug(f"[buy] grey-tile check skipped: {exc}")
        return False


def _find_material_tile(elements, material: str):
    """(cx, cy) of the goods-grid tile whose label matches `material` (OmniParser
    button in the left/centre grid, cx < ~1600). None if not on screen (out of
    stock / wrong tab)."""
    # THE EXTRA WORD IS THE WHOLE DIFFERENCE. This was `m in lab or lab in m`, and the game
    # is full of names that are prefixes of one another — Almond / Almond Oil, Duck / Duck
    # Meat, Olive / Olive Oil (user, 2026-09-05).
    #
    # Live 2026-09-05 at Lisboa, with BOTH tiles on screen and both read correctly:
    #
    #     button 'Almond'      @ (1450, 557)
    #     button 'Almond Oil'  @ ( 570, 798)   <- what the substring test returned, twice
    #
    # It bought ~1,020 Almond Oil for ~194,000 ducats and two blue gems, drained Lisboa's
    # Almond Oil shelf twice, and carried zero Almond. Nothing downstream could catch it: the
    # ledger, the goal counter and every log line say the name we ASKED for, never the name
    # on the tile that was tapped. The goal then read 1020/1015 — met — and the mission would
    # have gathered the other two materials and failed at the barter panel.
    #
    # EXACT FIRST, then per-word fuzzy. `same_good_name` requires the same number of words,
    # so a prefix can never stand in for the good itself, while a tile that OCRs as 'Almend'
    # still matches.
    from utils.fuzzy import same_good_name
    exact, fuzzy = None, None
    for e in elements:
        lab = (getattr(e, "label", "") or "").strip()
        if not lab or getattr(e, "cx", None) is None or getattr(e, "cx") >= 1600:
            continue
        is_button = (getattr(e, "element_type", "") or "") == "button"
        pos = (int(e.cx), int(e.cy))
        if lab.lower().split() == material.lower().split():
            if is_button or exact is None:
                exact = pos
        elif same_good_name(material, lab):
            if is_button or fuzzy is None:
                fuzzy = pos
    return exact or fuzzy


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

    # ACTION: tap each wanted good's tile (batch) → bulk-loads into the cart, then
    # PERCEIVE whether it actually staged — a live Purchase button carrying a cost.
    #
    # Re-tapping is allowed here and is not a flow sub-loop: same screen, our OWN
    # effect, and the cart says plainly whether it happened (CLAUDE.md, "the boundary
    # is the screen"). It is also SAFE in the one case it fires — an empty cart is
    # proof nothing was bought, so a retry cannot double-buy.
    #
    # It exists because a tap can be dropped: live 2026-09-02 the Candle tile at
    # Tripoli changed not one pixel, the cart stayed empty, and this returned "cart
    # may be empty" to a caller that logged none of it. TAP_DRIFT_MAX is the cause
    # and the fix; this is the backstop for whatever else drops one.
    tapped, not_found, greyed = [], [], []
    for material in targets:
        tile = _find_material_tile(els, material)
        if tile is None:
            not_found.append(material)
            continue
        # A GREYED TILE IS NOT TAPPED (user, 2026-09-02). Grey means sold out — the shelf
        # emptied, often because WE just bought it out — and a tap on it does nothing at
        # best. Live 2026-09-02 at Madeira round 1 bought the Raisin shelf dry and round 2
        # went on tapping the grey tile it left behind.
        if _tile_is_greyed(frame0, tile):
            logger.info(f"[{port}] {material} tile is greyed — sold out, not tapping it")
            greyed.append(material)
            continue
        logger.info(f"[{port}] load {material} — tap tile @ {tile}")
        tap_fn(*tile)
        tapped.append(material)
        time.sleep(settle)

    if not tapped:
        why = (f"{greyed} sold out" if greyed else f"no tiles found for {not_found}")
        return {"ok": False, "goal": dict(goal or {}), "tapped": [],
                "purchased": False, "cost": 0, "greyed": greyed,
                "reason": f"nothing to load: {why}"}

    time.sleep(settle)
    frame2 = capture_fn()
    els = omni_fn(frame2)
    commit = commit_fn(frame2, els)
    if commit is None:
        # A CART THAT WILL NOT FILL IS NOT A TAP THAT MISSED (live 2026-09-02, Madeira).
        #
        # Re-tapping here was mine, added the same morning, and it assumed a tile tap is
        # idempotent. It is not: TAPPING A STAGED TILE UN-STAGES IT. So when the commit
        # detector failed — it was rejecting a lit Purchase button at 0.279787 against a 0.28
        # cut — this "retry" toggled a cart that had been correctly filled on the first tap,
        # three times, and left 325 Raisin dangling. Back then raised "Moving to another menu
        # will empty the cart", which nothing could answer, and the mission died there.
        #
        # The retry only makes sense for a tap the GAME never saw, and this cannot tell that
        # from a commit button it merely failed to find. So it stops: report, and let the
        # caller look again with fresh eyes. One honest refusal beats three destructive taps.
        return {"ok": False, "goal": dict(goal or {}), "tapped": tapped,
                "purchased": False, "cost": 0,
                "reason": f"loaded {tapped} but found no Purchase button — the cart may be "
                          "staged already; NOT re-tapping, because a second tap un-stages it"}

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
        # THE OVERLOAD NOTICE, WHICH SAYS NONE OF THE WORDS BELOW (user, 2026-09-04: "for
        # this one you can tap the Ok button").
        #
        #   "The Cargo Hold's Trade Goods slot will be exceeded by 52 slots.
        #    Purchase the Trade Goods?"                              [Cancel] [OK]
        #
        # No "confirm", no "result", no "balance" — so this loop broke on its first pass and
        # left the dialog standing. `purchase_goods` then returned ok with purchased=False,
        # and the caller walked out of the market through the chromed title with the Notice
        # still up. Live 2026-09-04 at Madeira (frame 272) that abandoned the purchase
        # entirely: 105 slots were free and nothing was bought.
        #
        # It is a plain acknowledgement — the game hands back what fits and the rest is not
        # taken — and it spends no gems, so OK is the answer. Matched on BOTH phrases, never
        # on the bare word "notice", because a Notice is a shape and not a meaning.
        if "exceeded by" in txt and "purchase the trade goods" in txt:
            pos = find_text_button(tokens, "ok", min_ratio=0.85)
            if pos:
                logger.info("[buy] trade-goods overload Notice — OK (the hold takes what "
                            "fits)")
                tap_fn(*pos)
                confirmed = True
                continue
            logger.warning("[buy] overload Notice is up but its OK could not be found — "
                           "leaving it rather than tapping blind")
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

# How long a nearly-expired restock timer is worth waiting out after a refresh that did not
# confirm. Long enough for the race below, short enough that a leg never parks on a shelf.
_WAIT_OUT_RESTOCK_S = 90


def _timer_seconds(text) -> Optional[int]:
    """`'00.00:11'` -> 11. The restock clock, with OCR's separators taken as read.

    The glyphs come back inconsistently — '00:18.26', '00.28.40', '00.00:11' are all real
    readings of the same field — so the SEPARATORS carry no meaning and only the digit groups
    do: h:m:s, or m:s. Same rule as utils.digits: on these screens a separator is a separator.
    """
    groups = re.findall(r"\d+", str(text or ""))
    if not groups or len(groups) > 3:
        return None
    parts = [int(g) for g in groups][-3:]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, sec = parts
    return h * 3600 + m * 60 + sec


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

    # A SHELF ABOUT TO RESTOCK ITSELF IS NOT A REFUSED SHELF (user, 2026-09-05: "it tapped at
    # the refresh, but the market was refreshing at the time, so there is no blue gem dialog").
    #
    # Live at Madeira the ↻ went in with the timer reading 00.00:11. The game was already
    # turning the market over, so no Replenish-Stock dialog appeared, nothing could be
    # confirmed, and an unconfirmed refresh BREAKS the buy loop — the Raisin leg stopped at
    # 868 of 1,260 eleven seconds before the shelf refilled for free.
    #
    # The timer still does not GATE the refresh (user, 2026-09-05: "right now the timer
    # should not be used at all... it should not interfere with the refresh") — the tap has
    # already happened. It only answers the question that arises AFTERWARDS: having failed to
    # confirm, is this shelf dead, or about to refill by itself?
    if not ok:
        left = _timer_seconds(btn.timer)
        if left is not None and 0 <= left <= _WAIT_OUT_RESTOCK_S:
            logger.info(f"[refresh] not confirmed, but the restock timer read {btn.timer} "
                        f"({left}s) — waiting it out rather than calling the shelf empty")
            time.sleep(left + settle + 2.0)
            frame4 = capture_fn()
            again = _good_in_stock(frame4, verify_good, port, read_market_fn, omni_fn) \
                if verify_good else None
            if again:
                logger.info(f"[refresh] the shelf restocked on its own timer — "
                            f"tile[{verify_good}] is active again, no gem spent")
                return {"ok": True, "currency": None,
                        "reason": f"the restock timer came round ({btn.timer})"}
            logger.info(f"[refresh] still empty after waiting out {btn.timer}")

    return {"ok": ok, "currency": "blue_gem",
            "reason": f"refresh {'ok' if ok else 'NOT confirmed'} ({signal})"}


def _qty_of(goods: Mapping, name: str) -> Optional[int]:
    """A good's shelf quantity from a grid reading, or None when it was not read."""
    g = (goods or {}).get((name or "").lower())
    q = getattr(g, "available_qty", None) if g is not None else None
    return int(q) if isinstance(q, int) else None


def _shelf_drop(before: Mapping, after: Mapping, name: str) -> int:
    """How far one good's shelf fell across a buy — 0 when it cannot be said.

    Only meaningful for a SINGLE-good round; with two goods bought together the drop of one
    says nothing about the other, which is the same reason the tracked cargo tile cannot be
    credited to both (see the ledger call in `buy_to_goal`).

    A shelf that RISES has been refreshed rather than bought from, and a missing reading is
    not a drop of zero — both return 0 so the ledger keeps its "amount unknown" meaning
    rather than being told a fiction.
    """
    b, a = _qty_of(before, name), _qty_of(after, name)
    if b is None or a is None:
        return 0
    return max(0, b - a)


def tile_in_stock(good_obj) -> bool:
    """Is this Purchase tile ACTIVE — i.e. can it be bought right now?

    STATE, NOT TRANSITION (user, 2026-08-24). A shelf is empty in three independent ways at
    once: the tile greys out, its quantity shows 0, and a "Sold Out" stamp appears. Any of
    them is enough, and a stocked good is NEVER 0 unless it has been bought.

    The buy loop used to detect emptiness by watching a tile go grey ACROSS a buy. That only
    catches a shelf that empties while you are standing there. Arrive at an already-empty
    shelf — or exit the building and come back — and there is no transition left to see:
    Bordeaux 2026-08-24, Raisin already drained by an earlier run, tile greyed with 0 and a
    Sold Out stamp, and the loop tapped it anyway because the `sold_out` FLAG alone read
    False. Nothing was staged, the Purchase button stayed greyed at 0, and no refresh fired.
    """
    # UNREADABLE IS NOT ZERO. Requiring a positive quantity would skip a perfectly stocked
    # shelf whose badge failed to OCR, and burn a blue gem refreshing it. The GREYED-OUT tile
    # is what says "empty" (vision.market_reader sets `sold_out` from it), so an unknown
    # quantity defers to that; a quantity that IS readable and zero still settles it, because
    # a good on sale is never 0 unless it has been bought (user, 2026-08-24).
    # A GATED GOOD IS NOT BUYABLE AND NOT REFRESHABLE — see MarketGood.conditional.
    if getattr(good_obj, "conditional", False):
        return False
    if getattr(good_obj, "sold_out", False):
        return False
    qty = getattr(good_obj, "available_qty", None)
    return qty is None or qty > 0


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
    return tile_in_stock(g)


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


def _submenu_says(frame):
    """WHICH market sub-menu the chromed title names: 'sell', 'purchase', or None.

    `on_submenu("sell", ...)` only ever tested one hypothesis, so a Purchase title read as
    "not sell" — indistinguishable from "could not tell". Asking which it IS lets a Purchase
    title reject outright instead of falling through.

    A READING, SO IT ASKS THE READER. The two questions parted company on 2026-09-03, when
    `on_submenu` began also requiring the screen to be ACTIONABLE — a dialog dims the chrome,
    and a dimmed title is no place to act. That is right for a precondition and wrong here:
    this reports what the title SAYS, and `_on_sell_tab` weighs it against the commit button,
    which is the anchor that actually decides. Asked the other way, a legible 'Sell' under a
    dialog came back None — "could not tell" — about a title you can read perfectly well.
    """
    try:
        from actions.ui import active_submenu
        title = (active_submenu(frame) or "").strip().lower()
        if title in ("sell", "purchase"):
            return title
    except Exception as exc:
        logger.debug(f"[buy_to_goal] title read failed: {exc}")
    return None


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
    # THE TITLE CHANGES FIRST AND MEANS LEAST. It used to be the FIRST anchor, returning True
    # the moment it read "Sell" — but the title flips as soon as the tab is tapped, while the
    # grid and the commit button follow a beat later. That window is exactly when the answer
    # matters, and in it the title is wrong.
    #
    # Live 2026-08-30 at Faro, frame 0003: title 'Sell', right panel 'Purchase', grid still
    # the shop's. This returned True, the caller read the PURCHASE grid as the hold, and
    # loaded Ammo, Chicken Meat and Turron — goods the fleet had never bought — into a sell
    # basket. There was no Sell button to press (nothing was owned), so the leg reported
    # "sold: nothing" and abandoned the loaded basket, which then blocked the exit.
    #
    # So: the COMMIT BUTTON decides, which is what this docstring always said. The title only
    # corroborates it, or answers when the button cannot be read at all.
    title = _submenu_says(frame)                      # 'sell' | 'purchase' | None
    if title == "purchase":
        return False                                  # fast reject; no need to look further

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
    if any("sell" in l for l in labels):
        return True

    # The button said neither. Only now is the title worth anything, and a bare title is a
    # weak yes — it is the corroboration, not the evidence.
    if title == "sell":
        logger.debug("[buy_to_goal] no commit button read; trusting the title alone")
        return True
    return False


def ensure_sell_tab(capture_fn, tap_fn, settle: float = 1.2) -> bool:
    """Get onto the market's Sell tab and CONFIRM it. The one way to do this.

    There were two. This one — element first, calibrated point only as a fallback, verified
    afterwards — and `sell_goods`'s, which tapped `MARKET_COORDS["sell"]` blind and checked
    nothing. Live 2026-09-01 at Luanda that point landed in the dead space BETWEEN 'Purchase'
    and 'Sell', so the clear never left the Purchase grid; it read nothing sellable, called
    the hold clear, and the mission sailed to Tripoli with 4,448 Bambara Groundnut aboard and
    wedged there with a full hold. The correct version was a hundred lines away in this file.

    Tapping "Sell" while the Sell page is already OPEN is not a no-op: the page TITLE is also
    the word "Sell", beside the back arrow, and that is what a label search matches — live
    2026-08-22 the tap landed at (107,53) on the header of a Sell page showing Ebony 700 /
    Coral 797 and navigated away. So: only switch when we are not already there.

    Returns True only when `_on_sell_tab` agrees. A False is a REFUSAL, not an empty hold —
    callers must not read that as "nothing to sell".
    """
    frame = capture_fn()
    if _on_sell_tab(frame):
        return True

    pos = _sell_menu_item(frame)
    if pos is None:
        logger.warning("[market] no 'Sell' in the left menu — not guessing where it is")
        return False
    tap_fn(*pos)
    time.sleep(settle)
    frame = capture_fn()
    if _on_sell_tab(frame):
        return True

    # OUR OWN TAP RAISED A CONFIRM, SO COMPLETE IT (live 2026-09-01 at Tripoli). Switching
    # tabs with goods staged makes the game ask "Moving to another menu will empty the cart.
    # Continue?" — the same dialog `abandon_basket_confirm` names. The tab does not change
    # until it is answered, so refusing here leaves a leg failed over a question nobody
    # replied to: the trim reported "could not reach the Sell grid", the dispatcher cleared
    # the dialog a few seconds later, and the switch then completed with the mission already
    # dead. A dialog the bot's OWN action provoked is COMPLETED, never left for someone else.
    ok = _cart_confirm_ok(frame)
    if ok is not None:
        logger.info("[market] the staged cart raised a confirm — answering it, the cart is "
                    "the shop's, not the hold's")
        tap_fn(*ok)
        time.sleep(settle)
        frame = capture_fn()
        return bool(_on_sell_tab(frame))

    # NOTHING EXPLAINS THE UNCHANGED SCREEN, SO THE TAP WAS SIMPLY DROPPED — try once more.
    #
    # This game drops roughly one tap in twenty: measured 3 of 62 in a single mission, all
    # with nothing to distinguish them from the 59 that landed. One re-tap takes ~5% to
    # ~0.25%, which is the same arithmetic the dispatcher's own retry rests on.
    #
    # Live 2026-09-04, the SAME point, minutes apart and both from `_sell_menu_item`:
    #     Faro    frame 66  tap (65,274) -> frames 67, 68, 69 all still the Purchase page
    #     Madeira frame 170 tap (65,274) -> frame 171 on the Sell page
    # Identical coordinates and identical sequence; one landed and one did not.
    #
    # Faro's cost the mission: with no Sell tab the owned counts were unreadable all leg, so
    # the ledger could not mark Pig met, and the buy loop took four whole shelves — 2,736
    # against a goal of 1,260. The surplus filled the hold, Raisin came home 391 short and
    # the barter lost a round.
    #
    # ONCE, and only on a re-read of the position: the menu may have redrawn, and a stale
    # point is what tapped dead space between 'Purchase' and 'Sell' at Luanda. Bounded here
    # rather than looped, because past one retry the screen is refusing rather than dropping.
    pos = _sell_menu_item(frame) or pos
    logger.info(f"[market] the Sell tab did not open and nothing asked us anything — "
                f"the tap was dropped; tapping {pos} once more")
    tap_fn(*pos)
    time.sleep(settle)
    return bool(_on_sell_tab(capture_fn()))


def _cart_confirm_ok(frame):
    """(x, y) of OK on the empty-the-cart confirm, or None. Never a blind OK.

    Answering an unidentified dialog is how a bot confirms a purchase it never chose, so this
    reads the SAME two words the `abandon_basket_confirm` interruptor keys on before it will
    touch anything. The button itself comes from DialogModel, the canonical dialog reader.
    """
    try:
        from vision.omniparser import parse_fast_cached
        words = " ".join((getattr(e, "label", "") or "") for e in parse_fast_cached(frame))
        low = words.lower()
        if "cart" not in low or "empty" not in low:
            return None
        from actions.market_actions import _dialog_ok_pos
        return _dialog_ok_pos(frame)
    except Exception as exc:                       # noqa: BLE001 — a miss is a refusal
        logger.debug(f"[market] cart-confirm read failed: {exc}")
        return None


def _sell_menu_item(frame):
    """(cx, cy) of 'Sell' in the market's LEFT MENU, or None. Never a remembered point.

    IDENTIFY BY ASSOCIATION, NOT BY A COORDINATE (user, 2026-09-01). The word "Sell" appears
    at least three times on this screen and only one of them switches tabs:

        'Sell'           @ (65,274)    the left menu   <- the only one that navigates
        'Sell Supplies'  @ (1322,1009) dumps water and food
        'Sell Overload'  @ (1560,1009)
        (plus the page TITLE, which is the BACK control)

    A whole-page label search picked the right one on the frame it was measured against and
    would happily take 'Sell Supplies' on a frame where the bare 'Sell' read badly — selling
    the fleet's supplies to switch a tab. `detect_left_menu` clusters the left column and
    those two sit at y≈1009, outside it, so they cannot be chosen at all.

    THE CALIBRATED POINT IS GONE. `MARKET_COORDS["sell"]` was (65,225); the item is at
    (65,274) — 49px high, in the dead space between 'Purchase' and 'Sell'. It was measured
    once against a layout the game has since re-baked, which is the standing argument against
    writing down a number that means "where on the screen". A menu we cannot find is a
    REFUSAL, not a guess: `ensure_sell_tab` returns False and the caller declines to report
    the hold rather than tapping a remembered pixel.
    """
    from vision.omniparser import parse_fast_cached
    from vision.region_detectors.left_menu import detect_left_menu
    try:
        w, h = frame.size
        menu = detect_left_menu(list(parse_fast_cached(frame)), w, h)
    except Exception as exc:                       # noqa: BLE001 — a miss is a refusal
        logger.debug(f"[market] left-menu read failed: {exc}")
        return None
    for item in (getattr(menu, "items", None) or []):
        d = item if isinstance(item, dict) else getattr(item, "__dict__", {})
        if (d.get("label") or "").strip().lower() == "sell":
            return int(d["cx"]), int(d["cy"])
    return None


def _read_owned_via_sell(capture_fn, tap_fn, settle: float = 1.2) -> dict:
    """Owned units per good, read from the SELL tab (its count overlay is OCR'd reliably by
    read_market_page_omni, unlike the Purchase-side HOG panel).  Switches to the Sell tab, reads,
    returns {name_lower: qty}.  Empty dict on any failure (caller then just buys)."""
    from vision.market_reader import read_market_page_omni
    try:
        if not ensure_sell_tab(capture_fn, tap_fn, settle):
            # Reading the PURCHASE grid here returns the SHOP'S stock as if it were ours.
            # Live 2026-08-22 at Kolkata that answered "textiles: 3" while the hold carried
            # 920, so the pre-check decided it had to buy and loaded Textiles one at a time.
            # Unknown must stay unknown.
            logger.warning("[buy_to_goal] could not confirm the Sell tab — refusing to "
                           "report owned counts (a Purchase-grid read would be shop stock)")
            return {}
        # ALL PAGES, NOT THE VISIBLE ONE.
        #
        # The owned quantity is printed on the good's tile in the Sell grid — this is the
        # right place to read it. But the grid shows ONE 3x3 page, and a cluttered hold puts
        # the good below the fold. Live 2026-08-26 the grid read `Iron 1,451` on page two
        # while this function returned {} and `buy_to_goal` reported `0/470` for round after
        # round, buying past 2,000 against a goal of 470. The number was on screen the whole
        # time, one scroll away.
        #
        # `read_market_all_pages` already scrolls and accumulates — it existed the whole time
        # and this caller used the single-page reader beside it.
        from vision.market_reader import read_market_all_pages
        rows = read_market_all_pages(capture_fn, tab="sell")
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


def enough_already(ledger, track: bool, material: str, want: int) -> bool:
    """Has THIS material reached its own target?

    Unknown is never "enough": the ledger records an unreadable purchase as pending exactly
    so the caller goes and looks at the sell grid. Treating unknown as satisfied would turn
    "I could not read it" into "I have plenty", which is the opposite of safe.
    """
    if not track:
        return False
    try:
        if ledger.amount_unknown(material):
            return False
        return int(ledger.believed(material) or 0) >= int(want)
    except Exception:
        return False


def buyable_now(goal: Mapping[str, int], goods: Mapping, ledger, track: bool) -> list:
    """Which materials are worth tapping HERE, right now.

    Three things disqualify one, and the third is the one this did not use to check:
      * it is not on this port's grid at all,
      * its shelf is empty (that routes to the refresh path instead),
      * IT IS ALREADY DONE.

    Live 2026-08-30 at Faro, which stocks Pig but not Raisin. Pig passed its goal at
    1,344/1,260 and Raisin — the only shortfall left — was absent from the grid, so Pig was
    the only thing left in the list. The loop exits only when EVERY good is met, so it bought
    Pig again and again, 1,568 then 1,792, a blue gem per restock, filling the hold that
    Raisin needed for a material this port was never going to have.
    """
    out = []
    for material in goal:
        good = goods.get(material.lower())
        if good is None or not tile_in_stock(good):
            continue
        if enough_already(ledger, track, material, goal[material]):
            continue
        out.append(material)
    return out


def material_states(ledger, goal: Mapping[str, int]) -> dict:
    """Where each material stands: {material: {"have", "want", "state"}}.

    `state` is "met", "short" or "unknown" — PER MATERIAL, which is the fact the mission
    needs and the one that used to be destroyed here. `_goal_met` built exactly this and
    then flattened it into an English sentence ("short: Candle 206/704"), so the only
    caller could learn THAT something was missing but never WHICH.

    What that cost (live 2026-09-02): a gather leg is settled by the whole-order verdict,
    so Barcelona — which stocks neither — reported the same False for "Candle is short"
    as Tripoli did, and `gather:Amsterdam` stayed open for Iron already aboard at 972/822.
    The comment on `_settle_gathers` records it as a known gap: "a partial buy says nothing
    about which material fell short — the result reports a total and a verdict, not a
    breakdown". It reported no breakdown because this function threw it away.

    UNKNOWN IS NOT MET, deliberately. The ledger marks an unreadable purchase pending so
    the caller goes and reads the sell grid; calling it satisfied would turn "I could not
    read it" into "I have enough".
    """
    out: dict = {}
    for material, want in goal.items():
        want = int(want)
        if ledger.amount_unknown(material):
            out[material] = {"have": None, "want": want, "state": "unknown"}
            continue
        have = ledger.believed(material)
        out[material] = {"have": have, "want": want,
                         "state": "met" if have >= want else "short"}
    return out


def _goal_met(ledger, goal: Mapping[str, int]) -> tuple:
    """Is EVERY material at its own target? Returns (met, why).

    The verdict over `material_states`, so the two can never disagree.

    Never `sum(owned) >= sum(goal)`: a surplus of one material would cover a shortfall of
    another, which is exactly how a run left Barcelona holding 830 Iron (goal 506) and 158
    Matchlock Gun (goal 305) with the loop reporting success (2026-08-27).
    """
    states = material_states(ledger, goal)
    unknown = [m for m, s in states.items() if s["state"] == "unknown"]
    short = [f"{m} {s['have']}/{s['want']}"
             for m, s in states.items() if s["state"] == "short"]
    if unknown:
        return False, f"amount unknown for {', '.join(unknown)} — the sell grid must settle it"
    if short:
        return False, f"short: {', '.join(short)}"
    return True, ", ".join(f"{m} {s['have']}/{s['want']}" for m, s in states.items())


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
    # What THIS call has put aboard, measured by the CARGO TOTAL rather than the good's own
    # tile — the counter is in the right panel and stays legible when the tile is not.
    session_bought = 0
    session_cargo_at_start = None
    # FLEET holdings (authoritative, from the sell grid) kept apart from THIS VISIT'S
    # purchases (pending, until a sell-grid read absorbs them). See brain.market_ledger.
    from brain.market_ledger import MarketLedger
    ledger = MarketLedger()
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
        """Blue-gem refresh to restock a sold-out shelf; append to rounds. Returns ok.

        SAY WHY WHEN IT DOES NOT HAPPEN. A False here BREAKS the buy loop, and it used to do
        so in silence: `refresh_market`'s refusals all return without logging, so the run
        jumped from "not met — short: Mutton 595/1015" straight to "no further progress" with
        nothing in between. Live 2026-09-05 at Antalya that hid a one-frame OCR miss on the
        restock timer — the shelf was sold out, the control was on screen at 3 blue gems, and
        the leg ended three barter rounds short. It took a frame-by-frame diff to find, and
        the reason was a string the function already had in hand.
        """
        r = refresh_fn(capture_fn=capture_fn, tap_fn=tap_fn, verify_good=verify_good, port=port)
        rounds.append({"attempt": attempt, "refresh": r})
        if not r.get("ok"):
            logger.warning(f"[buy_to_goal] no refresh for {verify_good!r} — "
                           f"{r.get('reason', '(no reason given)')}; the shelf stays empty "
                           "and this leg stops here")
            return False
        return True

    goal_total = sum(goal.values())
    bought_total = 0              # best-known OWNED count of the good (from the tracked tile)
    tracked_pos = None            # remembered cargo tile of the good being bought
    refreshed_last = False        # did the previous round end in a refresh? (cargo-full detection)
    rounds: list = []

    def _met(total_seen: int) -> tuple:
        """Is the goal met? PER GOOD on the real path; summed only where no per-good
        reading exists.

        `cargo_fn` is a total-cargo stand-in with no notion of which good the units belong
        to, and it is injected only by tests — no production caller passes it (checked
        2026-08-27). The live path always runs the tracker, where the ledger holds a
        per-good count and a surplus of one material can never cover a shortfall of
        another.
        """
        if not _track:
            return (total_seen >= goal_total,
                    f"{total_seen}/{goal_total} (summed — no per-good reading on this path)")
        met, why = _goal_met(ledger, goal)
        if met or total_seen is None:
            return met, why
        # ONE GOOD IN THE ORDER MAKES AN AGGREGATE PER-GOOD. Every unit the cargo counter
        # saw arrive can only be this material, so a total is a per-good count here and the
        # loop must not stall waiting for a tile read it already has the answer to. This is
        # the ONLY case where a total may stand in: with two goods it is exactly the
        # attribution that failed at Barcelona (Iron's +324 credited to Matchlock Gun too).
        #
        # AND ONLY WHILE THE LEDGER CANNOT ANSWER. `total_seen` is the TRACKED TILE's
        # absolute count, not the arrivals this premise describes, so when the tracker
        # follows the wrong tile it is another good's number entirely. Live 2026-09-05 at
        # Madeira, ordering Raisin alone with 1,368 Pig already aboard:
        #
        #   bought 217 Raisin — believed 651        (three shelves, every amount READ)
        #   round 4/60: owned=1368 (+717)           (Pig's count, and no 717 shelf exists)
        #   goal met — Raisin ~1368/1260
        #
        # It reported `met` and `state: 'short'` in the same result, and the mission sailed
        # on believing Raisin gathered. A ledger with nothing pending has read every purchase
        # it made and IS the answer; overriding it with a tile that may have wandered is how
        # a leg reports a hold it does not have.
        only_good = next(iter(goal)) if len(goal) == 1 else None
        if only_good is not None and ledger.amount_unknown(only_good):
            only, want = only_good, goal[only_good]
            if total_seen >= int(want):
                return True, (f"{only} ~{total_seen}/{want} — the only good in the order and "
                              "its amounts are unread, so the cargo total is its count")
        return met, why

    # COLD pre-check via the SELL tab (no buying): how many do we ALREADY own?  Read from the Sell
    # tab's count overlay — reliable, unlike the Purchase-side HOG panel which mis-OCRs multi-digit
    # counts (live 2026-08-19: real 1,444 Textiles read as 14444/444).  If the ship already holds
    # the goal (PER GOOD, so a surplus of one material can't mask a shortfall of another), skip
    # buying / burning gems.  Then re-open the Purchase grid for the buy loop.
    if _track:
        owned = read_owned_fn(capture_fn, tap_fn, settle) or {}
        # The fleet's holdings, authoritative: the sell grid is the only place a per-good
        # quantity is legible. Seeding, not reconciling — nothing has been bought yet.
        ledger.seed(owned)
        # PER GOOD, NEVER THE SUM — the rule this file states twice, and the seed broke it.
        # `bought_total` is "best-known OWNED count of THE GOOD (from the tracked tile)", one
        # good, but this seeded it with the sum across EVERY good in the order. Every later
        # single-tile reading was then compared against that sum:
        #
        #   live 2026-08-31, Bordeaux, order {Raisin 1008, Pig 870}, tiles 704 and 916
        #     bought_total = 704 + 916 = 1620
        #     round 1: owned=1077 -> got = 1077 - 1620 = -543
        #     round 2: owned= 916 -> got =  916 - 1620 = -704
        #
        # A negative "rise" told the loop it was going backwards; it stopped with "no further
        # progress" and walked out of the market leaving a staged cart, which wedged the run.
        # A one-material order hides this entirely, because then the sum IS the tile.
        #
        # The tracked good is the first buyable one, so seed from THAT good's own count.
        pre_owned = max((owned.get(m.lower(), 0) for m in goal), default=0)
        if pre_owned:
            bought_total = pre_owned
            rounds.append({"pre_owned": pre_owned, "owned": owned})
            if all(owned.get(m.lower(), 0) >= q for m, q in goal.items()):
                return {"ok": True, "met": True, "bought_total": bought_total,
                        "goal": dict(goal), "goal_total": goal_total, "rounds": rounds,
                        "materials": material_states(ledger, goal),
                        "reason": f"already own {owned} ≥ goal"}
        show_grid_fn()                       # pre-check left us on the Sell tab → back to Purchase

    for attempt in range(max_rounds):
        frame, goods, els, cleared = _read_grid()
        # The session baseline is taken BEFORE the first purchase, or the first round's units
        # are lost and the loop stops a round late.
        if session_cargo_at_start is None:
            session_cargo_at_start = _safe_cargo_total(frame)
        if cleared:
            rounds.append({"attempt": attempt, "cleared_blocker": True})
        # Buyable = on the grid AND not stamped Sold Out. We do NOT gate on available_qty
        # (the reader parses that stock badge intermittently — 173 vs None live 2026-08-18).
        # ASK WHETHER THE TILE IS ACTIVE, not merely whether a flag says "sold out".
        # `tile_in_stock` is the same test the refresh already verifies with; using it here
        # too means an already-empty shelf routes to the refresh path instead of being tapped
        # fruitlessly (see tile_in_stock for the Bordeaux case).
        # A MET GOOD IS NOT BUYABLE. This filtered on "is it on the grid" and "is the tile in
        # stock" and never on whether the material had already reached its own target — so a
        # good stayed buyable after it was done. Live 2026-08-30 at Faro, which stocks Pig but
        # NOT Raisin: Pig passed its goal at 1,344/1,260 and Raisin was the only shortfall
        # left, but Raisin was absent from the grid and so filtered out, leaving Pig as the
        # only buyable thing. The loop exits only when EVERY good is met, so it bought Pig
        # again and again — 1,568, 1,792 — spending a blue gem per restock chasing a Raisin
        # this port was never going to have.
        #
        # `_goal_met` has always been per-good; the buy filter simply was not.
        buyable = buyable_now(goal, goods, ledger, _track)
        # ONLY A GENUINELY EMPTY SHELF IS WORTH A BLUE GEM. Empty means "not in stock for a
        # reason a refresh fixes": greyed out, or a readable 0 (a good on sale is never 0
        # unless it has been bought). A GATED good — time window, guild monopoly, nation
        # control — is also "not in stock", but no refresh can ever restock it, so counting it
        # here would spend gems on something that will never appear (user, 2026-08-24).
        def _refreshable(m):
            g = goods.get(m.lower())
            return (g is not None and not tile_in_stock(g)
                    and not getattr(g, "conditional", False))

        sold_out_needed = any(_refreshable(m) for m in goal)

        if buyable:
            snap_before = _cargo_tiles(frame, els) if _track else None
            cargo_before = None if _track else cargo_fn(frame)
            tiles = {m: find_tile_fn(els, m) for m in buyable}   # tile positions (before-buy)
            res = buy_round_fn(port, goods=list(buyable), capture_fn=capture_fn,
                               tap_fn=tap_fn, omni_fn=omni_fn, settle=settle)
            # SAY WHY A ROUND DID NOT BUY. purchase_goods computes a reason for every one
            # of its refusals — no tiles found, cart never filled, a red-gem price — and
            # this loop read only `ok`. Live 2026-09-02 at Tripoli that turned "the tap
            # never registered" into a silent stop, and the cause took a frame-by-frame
            # pixel diff to recover when it had been a string in hand all along.
            if not res.get("ok"):
                logger.warning(f"[buy_to_goal] round did not buy: {res.get('reason', '(no reason given)')}")
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
                # THIS ROUND'S RISE — and a rise cannot be negative. The line below already
                # guards the running total with max() "so a stale/failed read never DECREASES
                # the running total"; the DELTA computed from that same suspect number had no
                # such guard, and it is the one that reaches the ledger.
                #
                # Live 2026-08-31 at Bordeaux: a read came back 161 against a running 704, so
                # `got` was -543 — a number OmniParser never saw, produced by our own
                # subtraction. The ledger believed it, the loop read its accounting as going
                # backwards, stopped with "no further progress", and walked out of the market
                # leaving a staged cart, which then wedged the run on the cart dialog.
                #
                # A read below the total is a FAILED READ, not goods leaving the hold. None
                # means unreadable, which is the case the ledger exists for: it goes pending
                # and the sell grid settles it.
                if owned is None or owned < bought_total:
                    if owned is not None and owned < bought_total:
                        logger.warning(f"[buy_to_goal] owned read back {owned} against a "
                                       f"running {bought_total} — buying does not remove "
                                       "goods, so that read is wrong. Treating this round's "
                                       "amount as unreadable.")
                    got = None
                else:
                    got = owned - bought_total
            else:
                cargo_after = cargo_fn(after)
                got = (cargo_after - cargo_before) if (cargo_before is not None
                                                       and cargo_after is not None) else None
            # Progress = the OWNED count itself — "own N", stop when the ship holds enough.
            # max() so a stale/failed read never DECREASES the running total.
            if cargo_after is not None:
                bought_total = max(bought_total, cargo_after)

            # COUNT WHAT THIS SESSION BOUGHT — the bound that works when the read does not.
            #
            # A loop that cannot see the hold still knows exactly what it has PUT IN the hold
            # since it started, and that alone bounds it (user, 2026-08-26). It is a strictly
            # weaker claim than the owned count — it says nothing about what was already
            # aboard — which is why it can only ever STOP the loop early, never let it run on.
            #
            # Without it: `owned=UNREADABLE ... 0/470` for round after round while the ship
            # took on 2,000 Iron, ~558k ducats and a blue-gem refresh each time. The tile was
            # below the fold; the purchases were never in doubt.
            # The CARGO TOTAL is the reading that survives a cluttered hold: the load
            # counter sits in the right panel and was legible in every frame of the run that
            # went wrong, while the good's own tile was below the fold. Buying one good, its
            # rise IS the units bought.
            if res.get("ok"):
                # A CONFIRMED purchase — pending against the ledger until a sell-grid read
                # absorbs it. Recorded whatever the cargo panel says next, because the buy
                # itself is not what is in doubt.
                # `got` is the rise of ONE cargo tile — `track_bought_good` follows a
                # single tile by design. Crediting it to every good in the order is
                # fiction: live 2026-08-27 at Barcelona a round bought Iron 506→830 (+324)
                # and Matchlock Gun 0→158, and this recorded 324 for BOTH. The ledger then
                # believed 324 Matchlock when 158 were aboard — enough, on paper, for six
                # barter rounds it could feed two of.
                #
                # With one good in the order the tracked rise IS that good's. With more
                # than one, no per-good number was measured, so each is recorded as
                # bought-amount-unknown, which routes the loop to the sell grid — the only
                # panel where a per-good quantity is legible.
                if len(buyable) == 1:
                    # THE SHELF'S OWN QUANTITY IS READABLE WHEN THE OWNED COUNT IS NOT, and
                    # with ONE good in the order its drop IS what we bought.
                    #
                    # Live 2026-09-04 at Faro the Sell tab would not confirm, so every round
                    # logged `owned=UNREADABLE ... 0/2520` while the grid said plainly:
                    #
                    #     'Pig' owned was unreadable on the page; its tile reads 684
                    #     [Faro] load Pig — tap tile @ (1007, 555)
                    #     'Pig' owned was unreadable on the page; its tile reads 0
                    #
                    # 684 to 0 is 684 bought, and nothing about it depends on the panel that
                    # failed. Without it the ledger recorded "amount unknown", `buyable_now`
                    # could never mark Pig met, and the loop bought FOUR shelves — 2,736
                    # against a goal of 1,260 — stopping only on the summed runaway guard.
                    # The 1,476 surplus then filled the hold, left 105 free slots, and cost
                    # the mission its Raisin and a barter round.
                    #
                    # Strictly a fallback: a measured `got` is a real per-tile delta and
                    # always wins. This only speaks where the alternative is silence.
                    amount = int(got or 0)
                    if amount <= 0:
                        amount = _shelf_drop(goods, goods_after, buyable[0])
                        if amount:
                            logger.info(f"[buy_to_goal] owned unreadable, but {buyable[0]!r}'s "
                                        f"shelf went {_qty_of(goods, buyable[0])} → "
                                        f"{_qty_of(goods_after, buyable[0])} — crediting "
                                        f"{amount} bought")
                    ledger.bought(buyable[0], amount)
                else:
                    logger.info(f"[buy_to_goal] {len(buyable)} goods bought together "
                                f"({', '.join(buyable)}) — one tracked tile cannot say how "
                                "many of each; the sell grid will settle it")
                    for m in buyable:
                        ledger.bought(m)          # pending, amount unknown
                total_now = _safe_cargo_total(after)
                if total_now is not None and session_cargo_at_start is not None:
                    session_bought = max(session_bought, total_now - session_cargo_at_start)
                else:
                    session_bought += max(int(got or 0), 0)
            if session_bought >= goal_total:
                # A RUNAWAY GUARD, NOT A SUCCESS TEST. `session_bought` is a cargo-total
                # delta across every good, so it can reach the summed goal with one
                # material still short. Stopping here is right — it exists to bound a loop
                # whose reads have failed — but `met` must be answered PER GOOD, or the
                # caller is told the mission is supplied when it is not.
                session_met, why = _met(max(bought_total, session_bought))
                logger.info(f"[buy_to_goal] stopping: this session has bought ~{session_bought} "
                            f"of a {goal_total} goal — whatever the hold reads "
                            f"({'goal met' if session_met else why})")
                return {"ok": True, "met": session_met,
                        "bought_total": max(bought_total, session_bought),
                        "goal": dict(goal), "goal_total": goal_total, "rounds": rounds,
                        "session_bought": session_bought,
                        "materials": material_states(ledger, goal),
                        "reason": f"bought ~{session_bought}/{goal_total} this session"
                                  + ("" if session_met else f"; {why}")}
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
            # PER GOOD, never the sum. `bought_total` is a single good's owned count and
            # `goal_total` the sum of every goal, so one over-bought material silently
            # covered another's shortfall: live 2026-08-27 Iron 830 cleared a combined
            # 811 goal (506 Iron + 305 Matchlock) while only 158 Matchlock were aboard.
            # It then broke out of the loop BEFORE the sold-out branch below, so the blue
            # gem that would have restocked the empty Matchlock shelf was never spent.
            # The cold pre-check above always tested per good; the loop simply did not.
            met, why = _met(bought_total)
            if met:
                logger.info(f"[buy_to_goal] goal met — {why}")
                break
            logger.info(f"[buy_to_goal] not met — {why}")

            # A SOLD-OUT SIGNAL OUTRANKS AN UNREADABLE COUNT. `emptied` is a COLOUR reading
            # and does not depend on the count at all, so when the shelf has visibly gone
            # empty the refresh path below is the right recovery — stopping first would
            # abandon a run that a blue gem fixes. This ordering was wrong when the blind-stop
            # was added on 2026-08-24: it returned before the refresh branch could be reached.
            if emptied and (cargo_after is None):
                logger.info(f"[buy_to_goal] count unreadable but {emptied[0]!r} has gone "
                            "empty — taking the refresh path rather than stopping")
            else:
                # NEVER BUY BLIND. If the owned count cannot be read, the loop has no way to know
                # it is making progress — and it keeps SPENDING while it finds out. That is how
                # 1,681 Ebony got bought against a goal of 350 (2026-08-21), and it repeated at
                # Bordeaux on 2026-08-24: four Purchase taps at 149,695 ducats each while the
                # counter sat at 0/777, on course for all 39 rounds.
                # Stop on the FIRST unreadable round (user, 2026-08-24) and say what was actually
                # seen, so the next question — "is it tracking the wrong item, or no item at
                # all?" — can be answered from the log instead of another live run.
                strip_problem = None
                if _track and cargo_after is not None:
                    try:
                        strip_problem = cargo_read_problem(after, _cargo_tiles(after, els_after),
                                                           els_after)
                    except Exception as exc:
                        logger.debug(f"[buy_to_goal] cargo cross-check skipped: {exc}")
                if strip_problem:
                    # The count READ fine but the strip is only partly visible, so the number may
                    # belong to the wrong tile. Same rule as an unreadable count: stop, report.
                    logger.error(f"[buy_to_goal] cargo strip not fully read ({strip_problem}) — "
                                 "stopping rather than tracking a good the read cannot see")
                    return {"ok": False, "met": False, "bought_total": bought_total,
                            "rounds": rounds, "tracked": tracked_pos,
                            "materials": material_states(ledger, goal),
                            "reason": f"cargo strip only partly read: {strip_problem}"}
                if cargo_after is None and ledger.has_pending():
                    # THE RIGHT PANEL FAILED, SO GO AND LOOK PROPERLY (user, 2026-08-26).
                    #
                    # The purchases are not in doubt — each was confirmed by its result
                    # dialog. What failed is the cheap reading, and the SELL GRID is the panel
                    # that shows what the fleet actually owns. Re-read it, absorb the pending
                    # counts, and carry on buying; stopping here is what left the loop unable
                    # to tell 470 from 2,000.
                    logger.info(f"[buy_to_goal] the cargo strip did not read and "
                                f"{ledger.pending_goods()} are pending — re-reading the sell "
                                "grid to confirm what is aboard")
                    confirmed = read_owned_fn(capture_fn, tap_fn, settle) or {}
                    if confirmed:
                        ledger.reconcile(confirmed)
                        show_grid_fn()          # the read left us on the Sell tab
                        cargo_after = ledger.believed(next(iter(goal), ""))
                        bought_total = max(bought_total, cargo_after)
                        tracked_pos = None      # the tile moved; re-find it next round
                if cargo_after is None:
                    # Diagnostics must never be the thing that crashes the safe stop.
                    try:
                        after_tiles = list(_cargo_tiles(after, els_after) or [])
                    except Exception:
                        after_tiles = []
                    before_tiles = list(snap_before or [])
                    logger.error(
                        f"[buy_to_goal] owned count UNREADABLE after buying {', '.join(buyable) or '?'} "
                        f"— stopping rather than buying blind. Cargo tiles before="
                        f"{[n for _p, n in before_tiles]} after={[n for _p, n in after_tiles]}, "
                        f"tracked_pos={tracked_pos}. Same-count-before-and-after means the buy "
                        "did not register; NO tiles at all means the cargo strip was not read.")
                    return {"ok": False, "met": False, "bought_total": bought_total,
                            "rounds": rounds, "tracked": tracked_pos,
                            "materials": material_states(ledger, goal),
                            "reason": ("owned count unreadable — stopped to avoid buying blind; "
                                       "check whether the cargo strip is readable on this screen")}
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
                # A VISIBLY STOCKED SHELF IS NOT A STOCK PROBLEM, SO IT IS NOT WORTH A GEM
                # (user, 2026-09-04: "blue gem should only be used when the tile is greyed
                # out and stock is 0; on 259 it should not use blue gem to refresh as it is
                # still available").
                #
                # The speculative refresh below exists for a shelf the READER missed. When
                # the reader can see the shelf and it is stocked, buying nothing means the
                # hold could not take it — and no amount of restocking fixes that. Live
                # 2026-09-04 at Madeira, frame 259: Raisin 217 on the tile, fully active,
                # 105 free slots, and the hold full of the Pig surplus. The buy raised
                # "The Cargo Hold's Trade Goods slot will be exceeded by 52 slots"; the loop
                # read the 0 as a possible sold-out and spent a gem at 205 and rising, then
                # discovered the hold was full one round later anyway.
                stocked = [m for m in buyable
                           if (goods_after.get(m.lower()) is not None
                               and tile_in_stock(goods_after[m.lower()]))]
                if stocked:
                    logger.info(f"[buy_to_goal] bought 0 while {stocked[0]!r} is still in "
                                "stock — the shelf is not the problem, the room is; stopping "
                                "rather than spending a gem that cannot help")
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
        # Verify the refresh against the good that actually needs it — empty for a REFRESHABLE
        # reason. Picking by the `sold_out` flag alone left `vg=None` whenever the shelf was
        # empty by a readable 0, and a refresh with nothing to verify against cannot tell a
        # wasted gem from a working one (`refresh_stock` proves success by the tile going
        # active again). A gem that does not activate the tile returns ok=False and breaks the
        # loop, so at most ONE is spent per dead shelf.
        vg = next((m for m in goal if _refreshable(m)), None)
        if not _do_refresh(attempt, vg):
            break

    met = bought_total >= goal_total
    return {"ok": met or bought_total > 0, "met": met, "bought_total": bought_total,
            "goal": dict(goal), "goal_total": goal_total, "rounds": rounds,
            "materials": material_states(ledger, goal),
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


def cargo_read_problem(frame, tiles, elements=None) -> Optional[str]:
    """Why the cargo-strip read cannot be trusted, or None if it looks complete.

    A PARTIAL read is more dangerous than no read at all. `_cargo_tiles` returns whatever
    tiles it could resolve, and `track_bought_good` then compares before/after multisets over
    that subset — so a hold the bot cannot fully see looks like a hold that did not change,
    and the buy loop keeps spending. Measured at Bordeaux 2026-08-24: six tiles on screen
    (114, 980, 2, 170, 281, 281) and _cargo_tiles resolved ONE of them (170), because the
    middle tile came back from OmniParser labelled 'icon' with no number and broke the
    evenly-pitched run. The 980 was the Raisin being bought — invisible to the tracker.

    THE HOLD COUNTER IS THE CROSS-CHECK. The market HUD prints `used/capacity`, and when
    every tile is resolved their numbers SUM to `used` exactly (1,828 above). A shortfall
    means tiles are missing from the read — no inference required.

    An unreadable value is never just "unreadable": the screen is wrong, something is
    covering it, or the read is aimed at the wrong place (user, 2026-08-24). Naming which
    is the caller's job, so this reports the discrepancy rather than swallowing it.
    """
    # `_read_cargo_used_cap` returns None (not a pair) when the counter cannot be read, and
    # unpacking that raised TypeError — which `buy_to_goal` caught and logged at debug, so
    # THE CHECK SILENTLY DID NOTHING. Live at Gijón 2026-08-24 the tracker then reported the
    # Raisin tile's 980 as the count of the Pig it had just bought; it happened to clear the
    # goal either way, which is exactly the kind of luck that hides a defect.
    counter = _read_cargo_used_cap(frame)
    used = counter[0] if counter else None
    if used is None:
        # CANNOT VERIFY IS NOT THE SAME AS FOUND WRONG. This cross-check exists to catch a
        # PARTIAL strip read; with no counter there is nothing to compare against, so it has
        # no verdict to offer. Returning a problem here stopped a buy loop whose primary
        # signal was perfectly good — Bordeaux 2026-08-24, owned read 375 -> 623 (+248),
        # exactly right, halted because the counter happened to be unreadable on that frame.
        # The tracker's own None already covers "the count could not be read at all".
        logger.debug("[cargo] no cargo counter on this frame — cross-check skipped")
        return None
    seen = sum(n for _pos, n in (tiles or []))
    if seen != used:
        return (f"cargo tiles sum to {seen} but the hold holds {used} — "
                f"{len(tiles or [])} tile(s) resolved, so the strip is only partly read")
    return None


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
    from actions.sail_actions import _ocr_frame
    from utils.digits import parse_pair
    best = None
    for w, _c, x, y in _ocr_frame(frame, min_conf=0.3):
        # THE SEPARATOR IS WHATEVER THE READER SAW, not always a comma. This matched on
        # `[\d,]`, which a period breaks: '3.129/4,952' (live frame 236, 2026-09-05) could
        # only match '129/4,952', so the hold read 129 of 3,129 and the leg stopped two
        # barter rounds short. See utils.digits.
        pair = parse_pair(w)
        if pair and x > 1850:                    # cart panel, not Trade Points (left)
            cur, cap = pair
            if cap >= 500 and (best is None or cap > best[1]):   # largest cap = cargo hold
                best = (cur, cap)
    return best


def _safe_cargo_total(frame) -> Optional[int]:
    """`_read_cargo_total`, but a failure is None rather than an exception.

    This feeds the SESSION BOUND — a backstop that only ever stops the loop early. A backstop
    must not be able to bring down the thing it is backing up, and an unreadable counter is
    already a case the caller handles: it is no answer, not zero.
    """
    try:
        return _read_cargo_total(frame)
    except Exception as exc:
        logger.debug(f"[buy_to_goal] cargo total unreadable: {exc}")
        return None


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
