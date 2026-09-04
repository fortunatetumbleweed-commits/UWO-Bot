# actions/barter_executor.py
# P4 barter executors — verified, deterministic, driven by the SAFE tap primitive.
#
#   #24 barter_commit_verified — tap the yellow Exchange commit, confirm the result
#       dialog, and VERIFY the barter actually happened (amity changed OR cargo up),
#       like brain.verified_recruit. Does ONE commit and REPORTS — the caller (the
#       BarterMission #31) decides on no-progress, per "escalate, don't absorb".
#   #25 decide_negotiation / execute_negotiation — the "Attempt Negotiation" haggle
#       popup (No / Use 1 chance / Use all). Haggling, NOT a lottery; a FAILED try
#       REDUCES amity, so the default is to skip (carry surplus materials instead).
#
# All taps go through actions.adb_actions (input swipe) — never raw `input tap`.

from __future__ import annotations

import time
from typing import Callable, Optional, Tuple

from loguru import logger


# ── #24 Barter commit (verified) ───────────────────────────────────────────────

def _have_counts(snap: dict) -> list:
    """Each material's HELD count, in panel order. Positional, not by label: the OCR flickers
    between 'Luxuries' and 'uxuries' on the same tile, so matching by name invents changes."""
    mats = (snap or {}).get("materials")
    out = []
    for m in mats or ():
        if isinstance(m, (list, tuple)):
            out.append(m[1] if len(m) > 1 else None)
        else:
            out.append(getattr(m, "have", None))
    return out


def _barter_progressed(before: dict, after: dict) -> Tuple[bool, str]:
    """A barter happened if amity moved, cargo rose, OR MATERIALS WERE CONSUMED.

    THE FIRST TWO WITNESSES BOTH SATURATE, AND THEY SATURATE ON SUCCESS. Amity stops moving at
    the cap; cargo stops rising when the hold is full and the surplus is discarded. Reaching
    either is an achievement, and between them they can make a real round invisible.

    Live 2026-08-30 at Hutu Village, a fourth round with amity at 100,000/100,000 and cargo at
    4,952/4,952:

        amity=Friendly(100000, 100000)  materials=[('uxuries', 923, 219), ('Livestock', 1135, 219)]
        amity=Friendly(100000, 100000)  materials=[('Luxuries', 704, 219), ('Livestock',  916, 219)]

    Both counts fell by exactly 219 — the round plainly happened — but this returned "no
    amity/cargo change", the caller added a panel hidden behind a discard dialog, and the
    mission ended "the day's barter rounds are spent" with 3 of 6 rounds used and material for
    four more aboard.

    The materials were in the SAME snapshot all along (see `_read`, which records them). They
    are the witness that cannot saturate: every round consumes them.
    """
    # THE STRIP FIRST: it counts rounds outright. A NEW TILE IS A ROUND, full stop — no
    # delta that saturation can erase, and no whole-screen diffing, which cannot work here
    # anyway (the panel is translucent, with sea and ships moving behind it).
    r0, r1 = before.get("rounds"), after.get("rounds")
    if isinstance(r0, list) and isinstance(r1, list) and len(r1) > len(r0):
        return True, f"trade count {len(r0)}→{len(r1)} rounds{f' (+{r1[-1]})' if r1 else ''}"
    a0, a1 = before.get("amity"), after.get("amity")
    if a0 is not None and a1 is not None and a1 != a0:
        return True, f"amity {a0}→{a1}"
    c0, c1 = before.get("cargo"), after.get("cargo")
    if c0 is not None and c1 is not None and c1 > c0:
        return True, f"cargo {c0}→{c1}"
    b, a = _have_counts(before), _have_counts(after)
    if b and a and len(b) == len(a):
        spent = [(x, y) for x, y in zip(b, a)
                 if isinstance(x, int) and isinstance(y, int) and y < x]
        if spent:
            return True, "materials " + ", ".join(f"{x}→{y}" for x, y in spent)
    return False, "no round, amity, cargo or material change"


def _panel_is_open(panel) -> bool:
    """Is the barter panel still on screen? A closed submenu reads as nothing selected.

    The panel names the good and its output while it is up; once the game closes it, both
    are gone. Amity alone does not count — it is drawn on the village screen behind.
    """
    if panel is None:
        return False
    good = (panel or {}).get("good") if isinstance(panel, dict) else getattr(panel, "good", None)
    out = (panel or {}).get("out") if isinstance(panel, dict) else getattr(panel, "out", None)
    return bool(good) or out is not None


def _call_refresh(refresh_fn, panel) -> bool:
    """Call `refresh_fn`, passing the good when it wants one.

    `brain.barter_mission_live.refresh_stale_panel(good)` takes the good to reselect, and
    `village.py` passes it bare as `refresh_fn`, so calling it with no arguments raised
    TypeError — live 2026-08-27 that ended the run outright. The path had never been reached
    before, which is exactly how a signature mismatch survives.
    """
    good = (panel or {}).get("good") if isinstance(panel, dict) else getattr(panel, "good", None)
    try:
        return bool(refresh_fn(good) if good else refresh_fn())
    except TypeError:
        try:
            return bool(refresh_fn())
        except Exception as exc:
            logger.warning(f"[barter_commit] refresh_fn could not be called: {exc}")
            return False
    except Exception as exc:
        logger.warning(f"[barter_commit] refresh failed: {exc}")
        return False


def _short_materials(panel) -> list:
    """Materials the panel shows at zero — what a grey Exchange is usually waiting on.

    Reads the numbers already parsed off the right panel rather than looking again: a
    material at 0 is why the button is dead, and naming it turns "the day is over" into
    "buy this much and take the remaining round".
    """
    out = []
    mats = (panel or {}).get("materials") if isinstance(panel, dict) else getattr(panel, "materials", None)
    for entry in (mats or []):
        try:
            name, have = entry[0], entry[1]
        except (TypeError, IndexError):
            continue
        if have is not None and int(have) <= 0:
            out.append(str(name))
    return out


def barter_commit_verified(
    *,
    settle_secs: float = 2.5,
    capture_fn: Optional[Callable] = None,
    commit_fn: Optional[Callable] = None,
    confirm_fn: Optional[Callable] = None,
    read_panel_fn: Optional[Callable] = None,
    read_cargo_fn: Optional[Callable] = None,
    refresh_fn: Optional[Callable] = None,
    before_state: Optional[dict] = None,
) -> dict:
    """One verified Exchange commit on the (already-open) barter panel with a good
    selected. Returns {ok, reason, before, after, tapped}.

    `refresh_fn` closes and reopens the panel, reselecting the good, and returns whether it
    worked. It is called ONCE when a commit changes nothing, because the usual cause is a
    stale panel — see the note at the retry below. Omit it and the old behaviour stands.

    ok=False ⇒ the commit did NOT change amity/cargo; the caller escalates rather
    than blindly re-tapping."""
    if capture_fn is None:
        # THE ROUND'S READS COME FROM ONE OBSERVATION. `_state()` is called twice per attempt
        # — before the tap and after it — and each used to be a fresh capture and parse
        # (~4.6 s), which is most of the three-identical-reads-per-round measured at Hutu
        # Village on 2026-08-30. Through the repository the BEFORE is served from whatever the
        # caller already looked at, and the AFTER captures because the commit's taps told the
        # action layer the screen moved. The capture count becomes the number of things that
        # actually changed the screen.
        from actions.perception import screen
        capture_fn = lambda: screen().get(why="barter commit before/after").frame
    if read_panel_fn is None:
        from actions.barter_reader import read_barter_panel
        read_panel_fn = read_barter_panel
    if read_cargo_fn is None:
        from vision.hud_readers import read_cargo
        from vision.omniparser import parse_fast_cached
        read_cargo_fn = lambda frame: (lambda c: c[0] if c else None)(
            read_cargo(parse_fast_cached(frame)))
    if commit_fn is None:
        from brain.commit_actions import commit_via_positive_taps
        commit_fn = lambda: commit_via_positive_taps(goal_keywords=["exchange"])
    if confirm_fn is None:
        confirm_fn = _confirm_result_dialog

    def _state():
        frame = capture_fn()
        panel = read_panel_fn(frame)
        # CARRY WHAT THE PANEL SAYS, not just what changed. `good`/`out` are how we know the
        # submenu is still OPEN (the game closes it when the day's rounds run out), and
        # `materials` names the one at 0 when Exchange is grey. Reducing the read to
        # amity+cargo threw both away, so every question about WHY a commit did nothing had
        # to be answered by guessing.
        # AND THE STRIP. Each spent round leaves a tile carrying what it produced, so the
        # strip counts the day's rounds outright — the only signal here that states the
        # answer instead of implying it, and the only one that cannot saturate.
        try:
            from actions.barter_reader import read_trade_count
            rounds = read_trade_count(frame)
        except Exception as exc:
            logger.debug(f"[barter_commit] could not read the Trade Count strip: {exc}")
            rounds = None
        return {"amity": getattr(panel, "amity_points", None),
                "cargo": read_cargo_fn(frame),
                "good": getattr(panel, "good", None),
                "out": getattr(panel, "out", None),
                "materials": getattr(panel, "materials", None),
                "rounds": rounds}

    carried = [before_state]      # list so the nested _attempt can consume it

    def _attempt():
        # THE PREVIOUS ROUND'S `after` IS THIS ROUND'S `before`. Between them only a dispatcher
        # tick passes, and a tick that ACTS is a tick that would have changed the context away
        # from "panel ready" — so when the caller hands us its last `after`, reading the same
        # screen again buys nothing. `_state()` is the expensive call here: a capture, a panel
        # parse, a cargo read and a strip read, ~5s, and it was being paid twice per round.
        #
        # Only the FIRST attempt may carry it. A retry follows a commit that changed nothing,
        # which is exactly when the screen must be looked at afresh.
        before = carried[0] if carried[0] is not None else _state()
        carried[0] = None
        tapped = commit_fn() or []           # tap yellow Exchange (never red-gem)
        time.sleep(settle_secs)
        confirm_fn(capture_fn)                # OK the "Barter Calculations" result dialog
        time.sleep(1.0)
        after = _state()
        return before, after, tapped

    before, after, tapped = _attempt()
    progressed, why = _barter_progressed(before, after)

    # A GREY EXCHANGE MEANS ROUNDS REMAIN. This is the opposite of what it used to conclude.
    #
    # The two endings are told apart by the SCREEN, not inferred from the button (user,
    # 2026-08-27):
    #   * rounds USED UP  -> the game closes the barter submenu itself and drops the bot back
    #     to the village top menu. The panel is gone; there is nothing to read.
    #   * rounds REMAIN but the barter cannot proceed -> the panel stays open with Exchange
    #     GREY, because a material is short or amity is too low. The right panel names it:
    #     the short material shows 0 in RED.
    #
    # So reaching here — panel open, button dead — means a round is still available and
    # something is missing. Live 2026-08-27 at Svear this reported "the day's barter rounds
    # are spent" after 4 of 5 rounds, when Matchlock Gun had hit 0: the 5th round was there
    # for the taking, 95 Matchlock away, and the mission stopped believing the day was over.
    #
    # `exhausted` stays False for that reason — the day is NOT done, and a caller that can
    # restock may come back for the remaining round.
    if not progressed and not tapped:
        short = _short_materials(after)
        why_grey = (f"material short: {', '.join(short)}" if short
                    else "a material is short or amity is too low")
        logger.info(f"[barter_commit] Exchange is grey with the panel still open — rounds "
                    f"REMAIN today and {why_grey}. (Rounds running out closes the submenu "
                    "instead; this is not that.)")
        return {"ok": False, "exhausted": False, "blocked": True,
                "reason": f"Exchange grey — {why_grey}", "short": short,
                "before": before, "after": after, "tapped": tapped}

    # A STALE PANEL TAKES THE TAP AND DOES NOTHING. Measured at Svear Village on 2026-08-26:
    # the barter panel had been open since 09:22 with its refresh timer counting BACKWARDS
    # (-1:45:13), and its own numbers had quietly degraded — trade quantity 448(+98) -> 336(-14),
    # negotiation 9% -> 5%. Exchange raised the confirm dialog as normal, OK closed it, and
    # nothing happened: amity stayed at Neutral 60,000 against a promised Favorable 61,840.
    # Closing the panel and reopening it, with no other change, committed on the first try —
    # amity 60,000 -> 61,615, materials 445/146/438 -> 340/93/333.
    #
    # "Don't re-tap" was right: re-tapping a stale panel never works. But escalating was
    # wrong, because the remedy is to get a LIVE panel. That is this call's own precondition,
    # the way an open Buildings tab is `tap_building_entry`'s, so it is fixed here — and only
    # once, since a second failure means something other than staleness.
    # IS THE PANEL EVEN THERE? Ask before blaming staleness.
    #
    # The two endings differ by the SCREEN (user, 2026-08-27): when the day's rounds run out
    # the game CLOSES the barter submenu itself and drops the bot back to the village top
    # menu. There is no panel left — so a commit that "changed nothing" changed nothing
    # because there was nothing to commit ON, and reopening it is neither needed nor
    # possible. That is COMPLETION.
    #
    # Live 2026-08-27 at Hutu Village: the rounds ran out, the submenu closed, the Exchange
    # tap landed on the village menu behind it, and this read "no change" as a stale panel
    # and went to refresh — down a path that then crashed on a signature mismatch it had
    # never been reached to expose.
    if not progressed and not _panel_is_open(after):
        logger.info("[barter_commit] the barter submenu has CLOSED — the game shuts it when "
                    "the day's rounds run out. That is completion, not a stale panel.")
        return {"ok": False, "exhausted": True, "reason": "the day's barter rounds are spent",
                "before": before, "after": after, "tapped": tapped}

    if not progressed and refresh_fn is not None:
        logger.warning(f"[barter_commit] no change ({why}) — the panel may be stale; "
                       "refreshing it and committing once more")
        if _call_refresh(refresh_fn, before):
            before, after, tapped = _attempt()
            progressed, why = _barter_progressed(before, after)
        else:
            logger.warning("[barter_commit] could not refresh the panel")

    if progressed:
        from memory.observed_facts import forget
        forget("hold")     # a round consumed materials — the remembered hold is now wrong
        logger.info(f"[barter_commit] OK: {why}")
        return {"ok": True, "reason": why, "before": before, "after": after, "tapped": tapped}
    logger.warning(f"[barter_commit] NO PROGRESS: tapped {tapped}, {why} — escalate, don't re-tap")
    return {"ok": False, "reason": "barter commit made no change",
            "before": before, "after": after, "tapped": tapped}


def _confirm_result_dialog(capture_fn) -> bool:
    """Tap OK on the 'Barter Calculations' result dialog if present.  (Backstop —
    commit_via_positive_taps usually closes it already.)  NOTE: find_positive_button
    takes ELEMENTS + frame dims (the frame+keywords form was a long-dead signature that
    only surfaced live, 2026-08-20 — tests had this mocked)."""
    from brain.commit_actions import find_positive_button
    from vision.omniparser import parse_fast_cached
    from actions.adb_actions import tap
    frame = capture_fn()
    btn = find_positive_button(parse_fast_cached(frame),
                               frame_w=frame.width, frame_h=frame.height)
    # Only tap a DIALOG affirmative ('ok'/'confirm').  Without this gate the backstop could
    # return the panel's own Exchange button when no dialog is up → an unintended SECOND
    # barter commit (blind action on an assumed state).
    if btn is not None and any(k in (getattr(btn, "label", "") or "").lower()
                               for k in ("ok", "confirm")):
        tap(btn.cx, btn.cy)
        return True
    return False


# ── #25 Negotiation decision + executor ────────────────────────────────────────

# The haggle popup choices, by button text.
_NEGOTIATE_BUTTONS = {"no": ("no",), "once": ("use 1", "1 chance"),
                      "all": ("use all", "all remaining")}


def decide_negotiation(*, carrying_surplus_materials: bool, spare_cargo_space: int,
                       protect_amity: bool = True) -> str:
    """Decide how to handle the 'Attempt Negotiation' popup: 'no' | 'once' | 'all'.

    A failed negotiation REDUCES amity, so the default is conservative: skip it when
    protecting amity or when we carry surplus materials (just barter another round
    instead). Only haggle when we can't barter more (no surplus) and there's spare
    hold for the extra output."""
    if protect_amity or carrying_surplus_materials:
        return "no"
    if spare_cargo_space > 0:
        return "once"
    return "no"


def execute_negotiation(choice: str, *, capture_fn: Optional[Callable] = None,
                        tap_fn: Optional[Callable] = None,
                        find_button_fn: Optional[Callable] = None) -> dict:
    """Tap the negotiation-popup button for `choice` ('no'|'once'|'all')."""
    if capture_fn is None:
        from capture.adb_capture import capture_screen
        capture_fn = capture_screen
    if tap_fn is None:
        from actions.adb_actions import tap as tap_fn
    if find_button_fn is None:
        from actions.route_execution import find_text_button
        from actions.sail_actions import _ocr_frame
        find_button_fn = lambda kw: _find_negotiate_button(_ocr_frame(capture_fn()), kw)

    for kw in _NEGOTIATE_BUTTONS.get(choice, ("no",)):
        pos = find_button_fn(kw)
        if pos:
            tap_fn(*pos)
            return {"ok": True, "choice": choice, "tapped": pos}
    return {"ok": False, "choice": choice, "reason": "negotiation button not found"}


def _find_negotiate_button(tokens, keyword):
    from actions.route_execution import find_text_button
    return find_text_button(tokens, keyword, min_ratio=0.6)


# ── #26 Gifting executor (amity climb / recover) ───────────────────────────────
# Gift options: Friendship Token + blue-gem are REPEATABLE; the ducat gift is
# one-time (grayed once used). Prefer the Friendship Token (free-ish). NEVER a
# red-gem cost (not a gift option, but the currency gate stays on principle).
# From the walkthrough: after a successful gift the red note clears, the Gifting
# button disables, and amity rises (e.g. to Friendly).

_GIFT_PREFERENCE = ("friendship", "token", "gift token")


def gift_verified(
    *,
    settle_secs: float = 2.5,
    capture_fn: Optional[Callable] = None,
    read_amity_fn: Optional[Callable] = None,
    select_gift_fn: Optional[Callable] = None,
    gift_commit_fn: Optional[Callable] = None,
    confirm_fn: Optional[Callable] = None,
) -> dict:
    """One verified gift on the (already-open) Gifting panel. Selects the repeatable
    Friendship Token, commits Gift, confirms Yes, and VERIFIES amity rose.

    Returns {ok, reason, amity_before, amity_after}. ok=False ⇒ amity did not rise;
    caller escalates."""
    if capture_fn is None:
        from capture.adb_capture import capture_screen
        capture_fn = capture_screen
    if read_amity_fn is None:
        from actions.barter_reader import read_barter_panel
        read_amity_fn = lambda frame: getattr(read_barter_panel(frame), "amity_points", None)
    if select_gift_fn is None:
        select_gift_fn = _select_friendship_token
    if gift_commit_fn is None:
        gift_commit_fn = lambda: _tap_text_button(capture_fn, "gift")
    if confirm_fn is None:
        confirm_fn = lambda: _tap_text_button(capture_fn, "yes")

    before = read_amity_fn(capture_fn())
    select_gift_fn(capture_fn)
    time.sleep(0.8)
    gift_commit_fn()
    time.sleep(0.8)
    confirm_fn()
    time.sleep(settle_secs)
    after = read_amity_fn(capture_fn())

    if before is not None and after is not None and after > before:
        logger.info(f"[gift] OK: amity {before}→{after}")
        return {"ok": True, "reason": f"amity {before}→{after}",
                "amity_before": before, "amity_after": after}
    logger.warning(f"[gift] NO PROGRESS: amity {before}→{after} did not rise — escalate")
    return {"ok": False, "reason": "gift did not raise amity",
            "amity_before": before, "amity_after": after}


def _select_friendship_token(capture_fn) -> bool:
    from actions.sail_actions import _ocr_frame
    from actions.route_execution import find_text_button
    from actions.adb_actions import tap
    tokens = _ocr_frame(capture_fn())
    for kw in _GIFT_PREFERENCE:
        pos = find_text_button(tokens, kw, min_ratio=0.6)
        if pos:
            tap(*pos)
            return True
    return False


def _tap_text_button(capture_fn, keyword: str) -> bool:
    from actions.sail_actions import _ocr_frame
    from actions.route_execution import find_text_button
    from actions.adb_actions import tap
    pos = find_text_button(_ocr_frame(capture_fn()), keyword, min_ratio=0.7)
    if pos:
        tap(*pos)
        return True
    return False
