"""The village: barter until the village will not.

`brain.barter_mission_live.barter` is 241 lines and is BOTH the goal and the procedure — it
opens the panel, picks the tile, taps Exchange, confirms the dialog, clears overflow, counts
rounds, and decides whether to sail. Nearly every bug of 2026-08-26 lived in it.

THE GOAL CARRIES NO NUMBER. `Barter("Birch Tree", "Svear Village")` is the whole of it. The
pre-sail plan's round count was an estimate formed from a remote check hours old by the time
the fleet arrived, and it was systematically too small — it priced each round at its peak
footprint while a round actually FREES space (2026-08-23: 648 units of materials left, 497 of
product arrived, a net -151, so a plan of 1 against materials that funded 3). The panel is
ground truth and the panel bounds the loop.

WHAT COMES BACK IS TASK VOCABULARY: how many rounds, what the amity did, what materials are
left, and WHY IT STOPPED. `stopped_because` is the load-bearing field — "the village refused",
"the materials ran out" and "the good is not on offer today" are three different situations,
and none of them is a failure.

The loop here is allowed by the rule: it drives to a measurable target and terminates. "Until
the village refuses" is measurable — the Exchange button goes grey, and the game's answer
outranks any arithmetic in BOTH directions (grey means done even if the maths says another
round is fundable; live means continue even if the maths says it is not).

This activity never sails, never leaves the village, and never decides where to go next.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from loguru import logger

from brain import owned_state
from brain import village_context as _ctx
from brain.village_context import (BARTER_PANEL_BLOCKED, BARTER_PANEL_NO_GOOD,
                                   BARTER_PANEL_READY)
from brain.dispatcher import ActivityResult, BLOCKED, FINISHED, UNRECOGNISED, WORKING


# A safety stop, not a plan. Nothing should reach it — the panel stops the loop long before
# — and if anything ever does, that is a bug worth seeing rather than an all-night session.
_MAX_ROUNDS = 20


@dataclass(frozen=True)
class ReadBarterPanel:
    """What does this village's panel say about `good`, without committing anything?

    ENTRY BY PERCEPTION. Standing in the village, the ratio can be read off the panel — the
    remote check exists for a village the fleet has NOT sailed to, and using it here would
    mean opening the world map, which a village has no control for. So the bot would have to
    leave the very place it needs to be (live 2026-08-23).

    It reuses the Barter handlers exactly — open the submenu, select the good, read back —
    and differs only in the ENDING: it reports the panel instead of tapping Exchange.
    """
    good: str
    village: str = ""

    def __str__(self) -> str:
        return f"read the barter panel for {self.good!r}"


@dataclass(frozen=True)
class Barter:
    """Barter `good` here until the village will not. No quantity: see the module docstring."""
    good: str
    village: str = ""

    def __str__(self) -> str:
        return f"barter {self.good}" + (f" at {self.village}" if self.village else "")


# SEVEN, not eight. The strip draws eight slots, but the last is only reachable by PAYING for
# it (user, 2026-08-31), so seven is the ceiling for a day we actually play. More taps than
# that is a loop, not a barter — and this is only a loop guard: what decides whether there is
# another round is the Exchange button, which the game greys when there is not.
_MAX_DAILY_ROUNDS = 7


class VillageActivity:
    """Barter. Nothing else."""

    name = "village"

    SERVES = ("village", "sub_menu:barter")

    # A village is chromed — a back arrow, no ☰, NO GLOBE. Leaving is all it starts.
    CAN_START = ("EXIT_BUILDING",)

    # You leave a village to the SEA. There is no overworld under it.
    LEADS_TO = {"EXIT_BUILDING": "sea"}

    # WHICH ORDERS IT SERVES. Declaring none does not mean "none" — the dispatcher reads an
    # activity with no GOALS as a state-CLEARING one, "in the way of every goal and must run
    # whatever the order is". So this absorbed EVERY goal at a village and answered BLOCKED
    # to the ones it cannot do, which is the same failure its own comment below names and the
    # reason AshoreActivity was given GOALS explicitly. Found when `ClearOfTheVillage` was
    # swallowed here instead of being routed to a Back.
    GOALS: tuple = (Barter, ReadBarterPanel)

    # The screens this activity owns while it is running — its intent filter on SCREENS, the
    # way SERVES filters on states and GOALS on orders. Anything else is a MISS and an exit.
    CONTEXT_STATES = _ctx.CONTEXT_STATES

    # NOTE: `run_phase_fn` is GONE. It injected the flow sub-loop, and leaving it accepted
    # but unused would let a caller pass a stub that silently never runs — which is how four
    # test files kept asserting a contract the activity no longer had.
    _tick_frame = None
    _tick_panel = None          # the panel as read THIS tick — see `_panel`
    _tick_reading = None        # the RAW reading behind it, same tick

    def __init__(self, *, open_panel_fn=None, select_fn=None, read_panel_fn=None,
                 commit_fn=None, jettison_fn=None,
                 exchange_live_fn=None, recipe_fn=None, overflow_fn=None,
                 saw_fn=None, context_fn=None) -> None:
        self._open_panel = open_panel_fn
        self._select = select_fn
        self._read_panel = read_panel_fn
        self._commit = commit_fn
        self._jettison = jettison_fn
        self._exchange_live = exchange_live_fn
        self._recipe = recipe_fn
        # Injectable because the defaults CAPTURE THE SCREEN. Left un-injected, a unit test
        # reaches for the device (tests/conftest blocks ADB, so it does not steer the ship —
        # it just spends a minute failing to). A slow test is evidence, not an annoyance:
        # 18 tests went 0.2s -> 66s the moment `_what_it_saw` was added un-injected.
        self._overflow = overflow_fn
        self._saw = saw_fn
        self._context_fn = context_fn
        # Rounds committed in THIS activity's lifetime. Not a plan and not a bound — the
        # panel is the bound. It exists only so the report can say how many happened.
        self._committed = 0
        self._taps = 0
        self._last_after = None
        self._goal_key = None
        self._selects = 0

    # ── the one entry point: classify in context, take ONE action ────────────
    def work(self, goal: Any, state: Any) -> ActivityResult:
        """One action per call. The context says which; the dispatcher calls again.

        This replaced `_barter -> _loop -> run_barter_phase`, a `for _ in range(20)` that
        committed round after round without ever handing back. Nothing tells this bot that
        the world moved (Guiding Principle #0), so a loop that does not return cannot be
        told it is wrong — and on 2026-08-27 that loop kept tapping Exchange after the game
        had CLOSED the barter submenu, concluded "the panel must be stale", and crashed.

        Rounds are not counted here. The panel is the bound and the task runner owns the
        sequence; this call does one thing and says whether the goal is done.
        """
        # THE TICK'S FRAME. The handlers below are called with (self, goal) and never see
        # `state`, so without this each one captured a fresh screen — several captures inside
        # one tick, every one a DIFFERENT screen from the one that routed us here. It belongs
        # to the tick, the way `_goal_key` belongs to the goal.
        self._tick_frame = getattr(state, "frame", None)
        self._tick_panel = self._tick_reading = None    # a new tick, a new panel

        where = getattr(state, "state", None) or getattr(state, "location", None)
        if where is not None and where not in self.SERVES:
            return ActivityResult(UNRECOGNISED, {"state": where},
                                  detail=f"not in a village ({where!r})")
        if not isinstance(goal, (Barter, ReadBarterPanel)):
            return ActivityResult(BLOCKED, {}, detail=f"the village cannot serve {goal!r}")

        # THE COUNTER BELONGS TO THIS BARTER, NOT TO THE ACTIVITY. `default_activities()`
        # registers ONE VillageActivity and the dispatcher reuses it, so a count kept across
        # goals would report the previous mission's rounds as this one's — the same lifetime
        # mistake as any other datum outliving its owner (Guiding Principle #4). The goal IS
        # the owner here, so the count resets when the goal does.
        key = (getattr(goal, "good", None), getattr(goal, "village", None))
        if key != self._goal_key:
            self._goal_key, self._committed, self._selects = key, 0, 0

        local = self._classify(state)

        # AN OPEN PANEL IS NOT *OUR* GOOD. The context reads SCREENS; which good is selected
        # is a GOAL question, so it is answered here — the division the whole design rests
        # on (Guiding Principle #1: centralize the observation, localize the interpretation).
        #
        # Live 2026-08-26 at Svear the bot had Naverslojd selected while it wanted Birch
        # Tree. A panel full of Naverslojd's materials reads exactly like one full of Birch
        # Tree's: populated, with a good, and Exchange live. Only tapping and READING BACK
        # establishes which good it is, so a panel state that is not ours is re-selected
        # before anything is committed against it.
        if local in (BARTER_PANEL_READY, BARTER_PANEL_BLOCKED):
            wrong = self._wrong_good(goal)
            if wrong is not None:
                # AN ACTION THAT REPORTS SUCCESS BUT CHANGES NOTHING MUST NOT BE REPEATED.
                # `_select_trade_good` taps and reads back, so a True that leaves the panel
                # showing another good means the read and the tap disagree — and retrying
                # forever would be a WORKING loop that never ends, which is the sub-loop
                # failure re-created one level up.
                if self._selects >= _MAX_SELECT_ATTEMPTS:
                    return self._done(goal, f"selected {goal.good!r} {self._selects}x but "
                                            f"the panel still shows {wrong!r}", ok=False)
                logger.info(f"[village] the panel shows {wrong!r}, not {goal.good!r} — "
                            "selecting ours before acting on it")
                local = BARTER_PANEL_NO_GOOD

        # A READ ENDS WHERE A BARTER WOULD ACT. Everything up to here is shared — the same
        # handlers open the submenu and select the good, one step per tick — and the only
        # difference is that this one reports the panel instead of tapping Exchange.
        if isinstance(goal, ReadBarterPanel) and local in (BARTER_PANEL_READY,
                                                           BARTER_PANEL_BLOCKED):
            panel = self._panel()
            return ActivityResult(FINISHED,
                                  {"panel": panel, "good": goal.good,
                                   "rounds_remaining": getattr(panel, "rounds_remaining", None),
                                   "shortfall": getattr(panel, "shortfall", None)},
                                  detail=str(goal))

        handler = self._HANDLERS.get(local)
        if handler is None:
            # MISS — the hypothesis expired. Not an error: the normal way a context ends.
            saw = (self._saw or _what_it_saw)()
            return ActivityResult(UNRECOGNISED,
                                  {"state": "village", "context": local,
                                   "rounds_committed": self._committed, **saw},
                                  detail=f"no village screen matched (saw "
                                         f"{saw.get('screen') or 'unidentified'})")
        logger.info(f"[village] {local} -> {handler.__name__}")
        return handler(self, goal)

    def on_dialog(self, dialog, goal):
        """First refusal on a dialog covering the village. None means "not mine".

        THE ACTIVITY IS THE ONLY THING THAT KNOWS WHAT THE BUTTONS MEAN (see
        `docs/dialogs_are_windows.md`) — but knowing does not mean overriding. The unclaimed-
        goods prompt at the end of a barter round is ours, and the right answer to it is the
        DEFAULT one:

            Insufficient Empty Space - Cannot receive item due to insufficient space
            Notice - Complete the trade? 486 Bambara Groundnut has not been claimed yet.
                     Unclaimed trade goods will be discarded.        [Cancel] [Ok]

        OK finishes the round and acknowledges the loss. Cancel returns to the dialog behind,
        whose `Receive` cannot succeed with a full hold, which raises this same Notice again —
        a loop, because a village has no market to free space in. And "will be discarded" is
        what OK ACKNOWLEDGES, not what it causes: the goods are gone either way. This run had
        already taken that trade three times, discarding ~2,000 units to land 4,279.

        So this logs what it costs and hands back, rather than blocking a mission over a
        decision the game has already made for us.
        """
        text = " ".join(getattr(dialog, "body_text", ()) or ()).lower()
        if "has not been claimed" in text or "will be discarded" in text:
            logger.warning(
                "[village] the hold is full, so this round's output cannot be received — "
                "confirming acknowledges the loss and finishes the round. Cancelling would "
                "return to the same prompt with nowhere to put the goods. Said: "
                f"{' '.join(getattr(dialog, 'body_text', ()) or ())!r}")
        return None

    def _frame(self):
        """The frame for THIS tick — the dispatcher's, or a fresh one if it carried none."""
        if self._tick_frame is not None:
            return self._tick_frame
        from capture.adb_capture import capture_screen
        return capture_screen()

    def _classify(self, state) -> str:
        if self._context_fn is not None:
            return self._context_fn(state)
        from brain.village_context import classify
        frame = self._frame()
        # `panel=None` on purpose: the classifier reads the RAW panel itself. `self._panel()`
        # is the DERIVED state (rounds/binding/shortfall) and knows nothing about which good
        # is selected — passing it made every screen read as the top menu.
        return classify(frame, exchange_live=self._live_or_none())

    # ── one handler per context state; each does ONE thing and returns ───────
    def _on_top_menu(self, goal: Barter) -> ActivityResult:
        """The submenu is not up. Either we have not opened it, or the game CLOSED it
        because the day's rounds are spent — `_open_barter_panel` says which."""
        opened = (self._open_panel or _default_open)()
        if opened == "unavailable":
            return self._done(goal, "the village's barters for today are used up")
        if not opened:
            saw = (self._saw or _what_it_saw)()
            return ActivityResult(UNRECOGNISED,
                                  {"state": "village", "rounds_committed": self._committed,
                                   **saw},
                                  detail=f"the Barter panel would not open "
                                         f"(screen: {saw.get('screen') or 'unidentified'})")
        return ActivityResult(WORKING, {"rounds_committed": self._committed, "did": "opened the barter panel"}, detail=str(goal))

    def _on_no_good(self, goal: Barter) -> ActivityResult:
        """The panel opens with NOTHING selected. Selecting TAPS AND READS BACK — an open
        panel is not a selected good (live 2026-08-26: Naverslojd selected while Birch Tree
        was wanted, and its materials would have passed a laxer check)."""
        select = self._select or _default_select
        self._selects += 1
        picked = select(goal.good, self._recipe_for(goal.good))
        if picked is None:
            # The goods row could not be read. Say so and hand back — an unread panel is a
            # reason to look again, not a verdict on what the village is offering today.
            return ActivityResult(UNRECOGNISED,
                                  {"rounds_committed": self._committed, "good": goal.good},
                                  detail="the Barter panel's goods row could not be read")
        if not picked:
            return self._done(goal, f"{goal.good!r} is not on offer today", ok=False)
        return ActivityResult(WORKING, {"rounds_committed": self._committed, "did": f"selected {goal.good}"}, detail=str(goal))

    def _on_ready(self, goal: Barter) -> ActivityResult:
        """Exchange is live: commit ONE round, then hand back. The next perceive says what
        the round did — including whether it was the last one the day allows."""
        panel = self._panel()
        _log_shortfall(panel)
        before = _amity(panel)
        # HAND THE COMMIT WHAT WE ALREADY SAW. The previous round's `after` is this round's
        # `before` — between them only a dispatcher tick passed, and a tick that ACTED would
        # have moved the context off "panel ready". Re-reading it cost a capture, a panel
        # parse, a cargo read and a strip read every round, all to re-learn a screen nobody
        # had touched. Injected callers (tests) keep their old zero-argument shape.
        commit = self._commit or _default_commit
        try:
            res = commit(before_state=self._last_after) or {}
        except TypeError:
            res = commit() or {}
        self._panel_changed()          # the exchange moved materials and amity
        owned_state.changed(owned_state.FLEET, owned_state.PLACE)
        self._taps += 1

        # WHILE THE BUTTON IS GOLD, TAP IT. Ending is not this handler's call: the classifier
        # already routes a GREY Exchange to `barter_panel_blocked` and a CLOSED submenu back
        # to the village top menu, which are the only two real endings.
        #
        # This used to stop on `exhausted`, which `barter_commit_verified` infers from "the
        # round did not progress AND the panel is not open". Live 2026-08-30 at Hutu Village
        # both halves were wrong at once: amity sat at its 100,000 cap and the hold at
        # 4,952/4,952 with the overflow discarded, so a real round moved neither witness; and
        # the panel only looked closed because the "unclaimed goods will be discarded" dialog
        # was over it. The mission ended with the strip reading 4 of 6 rounds spent, Exchange
        # still gold, and material for four more aboard.
        #
        # A tap that changes nothing is cheap; a day thrown away is not. The only thing worth
        # guarding against here is tapping FOREVER, so bound it by what a day can hold.
        if self._taps > _MAX_DAILY_ROUNDS:
            return self._done(goal, f"tapped Exchange {self._taps}x — more than a day allows; "
                                    "stopping rather than tapping on")
        if not res.get("ok"):
            logger.info(f"[village] commit {self._taps} reported no change "
                        f"({res.get('reason')}) — the Exchange button decides whether there "
                        "is another round, so looking again rather than concluding")
            return ActivityResult(WORKING,
                                  {"rounds_committed": self._committed,
                                   "did": "committed, awaiting the panel"}, detail=str(goal))
        self._last_after = res.get("after")
        self._committed += 1
        # THE COMMIT ALREADY LOOKED. `barter_commit_verified` reads the panel on both sides to
        # decide whether the round progressed and hands back {before, after} — so re-reading
        # here was a capture + OmniParser pass (~2-3s) to fill in a log line, on the one path
        # where the cache has just been dropped because the exchange changed the panel.
        #
        # Same defect as `perceive()`-for-a-message earlier this session: the observation was
        # in hand and the code went and took another one.
        # BOTH SIDES FROM THE COMMIT'S OWN READS. It captured the panel before and after the
        # tap to decide whether the round progressed, so this is the closest reading to the
        # exchange that exists — and it costs nothing.
        logger.info(f"[village] round {self._committed} committed "
                    f"(amity {_amity(res.get('before')) or before} "
                    f"-> {_amity(res.get('after'))})")
        return ActivityResult(WORKING, {"rounds_committed": self._committed, "did": "committed a round",
                                        "rounds_committed": self._committed},
                              detail=str(goal))

    def _forget_the_carried_reading(self) -> None:
        """Only a round that just happened may hand its `after` to the next one. Any other
        path — a dialog, a reopen, a grey button — means the screen moved for a reason we did
        not cause, and a carried reading would describe a panel that no longer exists."""
        self._last_after = None

    def _on_blocked(self, goal: Barter) -> ActivityResult:
        """Exchange is GREY with the panel still OPEN, so rounds REMAIN and something is
        short — a material at 0, or amity too low. That is a different screen from the
        submenu the game closes when the rounds run out, and naming which one it is turns
        'the day is over' into 'buy this much and come back'."""
        self._forget_the_carried_reading()
        panel = self._panel()
        # SAY WHAT IS SHORT. A blocked panel is where the operator most needs this — it is
        # the difference between "the day is over" and "buy 95 more and come back".
        _log_shortfall(panel)
        short = [m for m, v in (getattr(panel, "materials", {}) or {}).items()
                 if isinstance(v, (list, tuple)) and v and not v[0]]
        why = (f"Exchange is grey — short: {', '.join(short)}" if short
               else "Exchange is grey — a material is short or amity is too low")
        return self._done(goal, why)

    def _on_confirm(self, goal: Barter) -> ActivityResult:
        """OUR OWN dialog, raised by our own Exchange tap — complete it, never dismiss it."""
        from brain.commit_actions import commit_via_positive_taps
        commit_via_positive_taps(goal_keywords=["ok", "confirm"])
        return ActivityResult(WORKING, {"rounds_committed": self._committed, "did": "confirmed the exchange"}, detail=str(goal))

    def _on_result(self, goal: Barter) -> ActivityResult:
        """The result dialog is the PROOF the round happened. Clear it and carry on."""
        from brain.commit_actions import commit_via_positive_taps
        commit_via_positive_taps(goal_keywords=["ok", "confirm"])
        return ActivityResult(WORKING, {"rounds_committed": self._committed, "did": "cleared the result dialog"}, detail=str(goal))

    def _on_overflow(self, goal: Barter) -> ActivityResult:
        """Units held PENDING because the hold is full. Dismissing this loses them."""
        pending = (self._overflow or _read_overflow)()
        jettison = (self._jettison
                    or _default_jettison(goal.good, self._recipe_for(goal.good),
                                         rounds_done=self._committed))
        jettison(pending)
        return ActivityResult(WORKING, {"rounds_committed": self._committed, "did": f"jettisoned for {pending} pending"},
                              detail=str(goal))

    # ── reporting ────────────────────────────────────────────────────────────
    def _done(self, goal: Barter, why: str, *, ok: bool = True) -> ActivityResult:
        panel = self._panel()
        return ActivityResult(
            FINISHED if ok else BLOCKED,
            {"rounds_committed": self._committed,
             "good": goal.good,
             "amity": _amity(panel),
             "materials_left": dict(getattr(panel, "materials", {}) or {}),
             "stopped_because": why},
            detail=str(goal))

    def _wrong_good(self, goal: Barter) -> Optional[str]:
        """The good the panel is showing, when it is not the one we came for — or a marker
        when the panel is populated and the good CANNOT BE READ.

        An unreadable good is not evidence that it is ours (Guiding Principle #3). A panel
        full of Naverslojd's materials reads exactly like one full of Birch Tree's, so
        "populated" says nothing about WHICH good, and only tapping and reading back does.
        The original code selected unconditionally for this reason; this keeps that, while
        skipping the tap in the one case where the panel positively names our good.
        """
        from brain.village_context import selected_good
        panel = self._reading()
        showing = selected_good(panel)
        if showing:
            return None if _same_good(showing, goal.good) else str(showing)
        if getattr(panel, "materials", None):
            # A REPEAT READ MEASURES THE READER, NOT THE PANEL. "unreadable" asks to confirm
            # BY SELECTING — but once we have selected, confirming means re-tapping the same
            # tile and re-reading the same label that already failed. That cannot produce
            # evidence it did not produce the first time; it only spends taps.
            #
            # Live 2026-09-05 at Berber: Argan Oil's name does not OCR on this panel on ANY
            # frame. `_select_trade_good` picked its tile correctly and accepted it on the
            # game's own live Exchange, and this then called the result "unreadable" and sent
            # it back to select again — six taps a minute, no rounds, then BLOCKED at the
            # attempt cap with every material aboard.
            #
            # The wrong-good protection is NOT weakened, because it does not live here: it
            # lives at the selection point, where there is still a choice to make.
            # `_try` matches the recipe first and REJECTS a tile that names another good,
            # falling back to the live Exchange only when our own reading is inconclusive.
            # After that there is nothing left for this check to decide.
            if self._selects:
                return None
            return "unreadable"          # populated, unnamed, and NOT ours to explain yet
        return None

    def _reading(self):
        """The RAW `BarterPanelReading` — which good is selected. Distinct from `_panel()`,
        the derived `PanelBarterState` used for rounds and materials.

        Cached for the tick like `_panel()`, and for the same reason: `_wrong_good` asks for
        it, and `_panel()` now derives from it rather than reading the screen a second time.
        Both views of the panel therefore come from ONE parse of ONE frame, and cannot
        disagree with each other.
        """
        if self._tick_reading is not None:
            return self._tick_reading
        if self._read_panel is not None:
            self._tick_reading = self._read_panel()   # tests inject one object for both
            return self._tick_reading
        try:
            from actions.barter_reader import read_barter_panel
            self._tick_reading = read_barter_panel(self._frame())
        except Exception as exc:
            logger.debug(f"[village] raw panel read failed: {exc}")
            return None
        return self._tick_reading

    def _live_or_none(self):
        live = self._exchange_live
        if live is None:
            return None                       # let the context reader decide
        try:
            return bool(live())
        except Exception:
            return None

    # ── why it stopped ───────────────────────────────────────────────────────
    def _why_it_stopped(self, res: dict, panel) -> str:
        """Three situations the task treats differently, and none is a failure.

        THE GAME'S ANSWER OUTRANKS THE MATHS IN BOTH DIRECTIONS. A greyed Exchange means done
        even when the materials look sufficient; a live Exchange means there is more to do
        even when they do not. Live 2026-08-23 the arithmetic said one more round was funded,
        the game disagreed, and the mission FAILED with its cargo aboard because a stall was
        read as an error.
        """
        if self._refused():
            return "the village refused — Exchange is greyed"
        if panel is not None and getattr(panel, "rounds_remaining", 0) == 0:
            return "the materials ran out"
        return res.get("reason") or "stopped"

    def _refused(self) -> bool:
        live = self._exchange_live
        if live is None:
            from actions.barter_panel import _exchange_still_live as live
        try:
            return not live()
        except Exception as exc:
            logger.debug(f"[village] could not read the Exchange button: {exc}")
            return False

    # ── helpers ──────────────────────────────────────────────────────────────
    def _panel(self):
        """The barter panel as read THIS TICK. Read once, shared by every caller.

        ONE OBSERVATION PER TICK (CLAUDE.md: "capture the screenshot once per tick, pass the
        same Image"). This read captures the screen and runs OmniParser — ~2-3s — and five
        call sites reach it inside a single tick: the goal check, `_on_ready`, `_on_blocked`,
        the shortfall log and the round-committed log.
        #
        # Measured at Svear 2026-08-30: SIX reads of the same panel per barter round, four of
        # them before a single tap, and one round took 57 seconds. The panel cannot change
        # between them — nothing has been tapped — so five of the six were pure cost.
        #
        # Scoped to the tick, not to the activity: the panel is PANEL-owned data (Guiding
        # Principle #4) and dies the moment anything is tapped. `work()` clears it.
        """
        if self._tick_panel is not None:
            return self._tick_panel
        read = self._read_panel
        if read is None:
            from actions.barter_panel import _read_panel_state
            # HAND OVER WHAT THIS TICK ALREADY PARSED. `_raw_panel()` reads the same screen
            # from the tick's frame, so deriving the state from that reading costs nothing
            # and the two views of the panel cannot disagree.
            raw = self._reading()
            read = lambda: _read_panel_state(self._frame(), reading=raw)
        try:
            self._tick_panel = read()
        except Exception as exc:
            logger.debug(f"[village] panel read failed: {exc}")
            return None
        return self._tick_panel

    def _panel_changed(self) -> None:
        """Something was tapped, so the panel we hold is stale. Drop BOTH views."""
        self._tick_panel = self._tick_reading = None

    def _recipe_for(self, good: str) -> Optional[dict]:
        if self._recipe is not None:
            return self._recipe(good)
        from brain import mission_progress
        prog = mission_progress.current() or {}
        recipe = prog.get("recipe")
        return (recipe or {}).get("materials") if isinstance(recipe, dict) else None


# THE HANDLER TABLE — the activity's intent filter on SCREENS. One entry per context state,
# each doing ONE action. Declared here, after the methods exist, so the states and their
# handlers cannot drift apart: a state with no handler is a MISS by construction.
VillageActivity._HANDLERS = {
    _ctx.VILLAGE_TOP_MENU:      VillageActivity._on_top_menu,
    _ctx.BARTER_PANEL_NO_GOOD:  VillageActivity._on_no_good,
    _ctx.BARTER_PANEL_READY:    VillageActivity._on_ready,
    _ctx.BARTER_PANEL_BLOCKED:  VillageActivity._on_blocked,
    _ctx.EXCHANGE_CONFIRM:      VillageActivity._on_confirm,
    _ctx.BARTER_RESULT:         VillageActivity._on_result,
    _ctx.OVERFLOW_PROMPT:       VillageActivity._on_overflow,
}
assert set(VillageActivity._HANDLERS) == set(VillageActivity.CONTEXT_STATES), \
    "every declared context state needs a handler, and vice versa"


# How many times to re-select before calling it a disagreement between tap and read.
_MAX_SELECT_ATTEMPTS = 2


def _same_good(a: str, b: str) -> bool:
    """Loose match — the panel's OCR of a name need not be byte-identical to the plan's.

    Loose about SPELLING, strict about WORDS. This stripped to alphanumerics and asked
    whether either contained the other, so 'almond' matched 'almondoil' — and the game is
    full of such pairs (Almond / Almond Oil, Duck / Duck Meat, Olive / Olive Oil). The same
    test in `_find_material_tile` bought 1,020 of the wrong good at Lisboa on 2026-09-05.
    """
    from utils.fuzzy import same_good_name
    return same_good_name(a, b)


def _what_it_saw() -> dict:
    """The screen the bot is actually looking at, for a caller that must reposition."""
    try:
        from actions.barter_panel import _no_panel_failure
        fail = _no_panel_failure() or {}
        return {k: fail[k] for k in ("screen", "submenu") if k in fail}
    except Exception as exc:
        logger.debug(f"[village] could not name the screen: {exc}")
        return {}


def _log_shortfall(panel) -> None:
    """A SHORTFALL IS INFORMATION, NOT A FAULT.

    When the panel funds fewer rounds than the hold was supposed to carry, the estimate has
    aged exactly as expected — a loss en route, or the ratio moved since the remote check. It
    is worth SAYING, because it tells the operator what happened. It is NOT returned as a
    field: the caller would only be able to compare it against a plan, and that comparison
    belongs to whoever made the plan (docs/architecture_DRAFT.md).
    """
    if panel is None:
        return
    binding, short = getattr(panel, "binding", None), getattr(panel, "shortfall", 0)
    rounds = getattr(panel, "rounds_remaining", None)
    logger.info(f"[village] the panel funds {rounds} full round(s)")
    if binding and short:
        logger.warning(f"[village] limited by {binding!r}; short {short} for one more round")
    partial = getattr(panel, "partial_fraction", 0) or 0
    if rounds == 0 and partial > 0:
        logger.info(f"[village] {partial:.1%} of a further round is fundable — not spending "
                    "a daily round on a partial")


def _amity(panel) -> Optional[tuple]:
    """Amity from whatever carries it — a panel reading, or the commit's own state dict.

    `barter_commit_verified` reads the panel either side of the tap and hands back
    `{"amity": ..., "cargo": ..., "good": ...}`; a `BarterPanelReading` exposes
    `.amity_points`. This accessor knew only the second, so the round log printed
    `amity None -> None` for every round of every run — the value was in hand and unread.
    """
    if panel is None:
        return None
    if isinstance(panel, dict):
        return panel.get("amity")
    return getattr(panel, "amity_points", None)


def _read_overflow() -> int:
    """Units the game is holding PENDING because the hold is full. Non-zero must be cleared
    before they are discarded — dismissing that dialog loses them."""
    try:
        from actions.overflow_dialog import read_overflow
        from capture.adb_capture import capture_screen
        from vision.omniparser import parse_fast_cached
        ov = read_overflow(parse_fast_cached(capture_screen()))
        if ov is not None and ov.pending:
            logger.warning(f"[village] OVERFLOW — {int(ov.pending)} unit(s) pending")
            return int(ov.pending)
    except Exception as exc:
        logger.debug(f"[village] overflow probe skipped: {exc}")
    return 0


# ── the defaults, which touch the device ─────────────────────────────────────

def _default_open():
    from actions.barter_panel import _open_barter_panel
    return _open_barter_panel()


def _default_select(good: str, recipe) -> bool:
    from actions.barter_panel import _select_trade_good
    return _select_trade_good(good, recipe)


def _default_commit(before_state=None) -> dict:
    from actions.barter_executor import barter_commit_verified
    from actions.barter_panel import refresh_stale_panel
    return barter_commit_verified(refresh_fn=refresh_stale_panel, before_state=before_state)


def _default_jettison(good: str, needs_per_round=None, rounds_done: Optional[int] = None):
    """THE ORDER OF SACRIFICE, decided in advance so nothing is escalated mid-round
    (user, 2026-08-26): dump the non-barter goods first; if that is not enough, spend supply
    down to a six-day floor; abandon barter goods only after both. Barter goods fetch very
    high profit, so the only real constraint is that the fleet must not run out of supply.

    `needs_per_round` is the recipe, and it is what lets the overflow module tell a MATERIAL
    from any other trade good. Without it materials are dumped on any round, which can throw
    away the inputs for every round still to come (user, 2026-09-04).

    `rounds_done` is this activity's committed count, which the overflow module needs for the
    one last-round test it cannot read off the dialog: the day's seventh round."""
    def jettison(pending: int) -> dict:
        from actions.overflow_dialog import clear_overflow
        from brain.supply_planner import supply_needed_each, VILLAGE_LEG_RESERVE_DAYS
        reserve = supply_needed_each(VILLAGE_LEG_RESERVE_DAYS)
        res = clear_overflow(output_good=good, needs_per_round=needs_per_round,
                             rounds_done=rounds_done,
                             reserves={"water": reserve, "food": reserve})
        if res.get("sacrificed"):
            logger.warning(f"[village] {res['sacrificed']} unit(s) of {good} given up — the "
                           "supply reserve could not be preserved any other way")
        logger.info(f"[village] overflow: {res.get('reason')}")
        return res
    return jettison
