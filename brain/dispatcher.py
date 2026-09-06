"""The state machine's dispatcher: it starts activities and converts goals into intents.

See `docs/architecture_DRAFT.md`. In one paragraph:

The dispatcher owns exactly two things — **state transitions** and **actions**. It does not run
the task. It starts an activity, regains control when that activity FINISHES, hands the result
up to the task runner, and converts the goal it gets back into the next intent. A transition
happens in exactly one place: when the dispatcher dispatches an intent. Nothing else moves the
bot between worlds — no sub-loop, no primitive, no recovery call buried inside an action.

    activity finishes
      -> dispatcher regains control
      -> RESULT goes up to the task runner
      -> task runner decides the next GOAL
      -> dispatcher converts it to an INTENT
      -> the intent starts the next activity

The granularity of the task runner's involvement is therefore the ACTIVITY BOUNDARY: never
every tick, never mid-activity. That is what keeps it free of screens and taps while still
letting it change its mind wherever changing it is possible.

After EVERY activity finishes, the dispatcher does two things — RE-PERCEIVE to learn where the
world is now, then CONSULT THE TASK RUNNER for the next goal. Those two steps are general, not
special-cased per state, which is why a state like the idle lock needs no wiring into any path
that might meet it: its whole repertoire is one action (swipe up), and when that returns the
activity has finished with nobody knowing where it landed. The normal loop discovers that. No
caller ever has to learn what a lock is.

An activity that loses its way hands control back rather than trying to recover, and the
dispatcher regains the bearings in a fixed order — PERCEIVE, TRANSITION to a state work can
happen in, REFRESH the data the wrong belief made stale — and asks the task runner LAST, and
only for a goal. It is never told to recover: recovery is a transition, and transitions are
the dispatcher's.

Why this exists: on 2026-08-25 a single call to `depart_from_port_via_world_map` held the goal
loop for six minutes. Inside it the primitive decided the departure had failed, walked to the
harbour, hunted a Depart button, tapped Supply Departure and re-selected the destination three
times — while perception correctly reported `port_overworld / Amsterdam` on every tick to
nobody who could act on it. The fleet had already ARRIVED. There was no boundary at which
anyone could notice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Callable, Mapping, Optional, Protocol

import time as _time

from loguru import logger

from brain import owned_state

# An activity ends in one of exactly three ways.
FINISHED = "finished"          # the work is done; `observed` says what came of it
BLOCKED = "blocked"            # could not do the work, but still knows where it is
UNRECOGNISED = "unrecognised"  # does not recognise this screen — end NOW, hand back
WORKING = "working"            # did ONE action; still in this context, goal NOT yet done


def still_working(result: Any) -> bool:
    """True while an activity is still SERVING the order it was given.

    THE ONE THING EVERY TASK RUNNER MUST AGREE ON. `WORKING` is this module's word for "did
    ONE action; still in this context, goal NOT yet done" — it is not success and it is not
    refusal, and a runner that reads it as either ends an order that was being served.

    Live 2026-09-04 at Svear: `BarterTaskRunner._absorb` treated anything that was not
    FINISHED as a refusal. The village activity opens the panel and selects the good on the
    first tick — its own docstring says "one step per tick" — and reports the panel on the
    second. The first tick's WORKING was read as "the panel did not answer here"; one second
    later the frame showed Birch Tree selected, 564 quantity, every material in surplus and
    Exchange gold. The command then replanned a whole mission whose materials were already
    aboard and died entering a market at a village.

    Four runners were interpreting this vocabulary at seven separate sites, each re-deriving
    it. `mission_runner` had it right and said so in a comment; `barter_runner` had it wrong
    for eight days and only failed when its path first became reachable; `sail_runner` logs
    "the course did not take" on every in-progress tick. This is that comment made into code.

    DELIBERATELY NARROW. What FINISHED means, and whether UNRECOGNISED is "still routing" or
    "not here — fall through", genuinely differ per runner and per goal: `sail_runner` reads
    FINISHED against the pending goal because "FINISHED means something different for each",
    and the barter PROBE treats UNRECOGNISED as its answer rather than an error. Those stay
    where they are. Only "still being served" is universal.
    """
    return getattr(result, "status", None) == WORKING
#
# WORKING is what lets an activity drop its flow sub-loop. It is the Android input-dispatch
# case: the activity handled an event, the screen changed, and it is STILL RESUMED — nothing
# transitioned. Without it, an activity that does one action per tick has only FINISHED to
# report, and `run_goal` reads FINISHED as "this goal is done" — which is exactly how a
# notice-dismissal retired a 253-unit purchase order on 2026-08-27.
#
# The distinction the three statuses now draw:
#   WORKING       one step taken, same context, ask me again      (no transition)
#   FINISHED      the GOAL is done                                (the task runner moves on)
#   UNRECOGNISED  this screen is not mine — perceive and route    (a transition happened)


@dataclass(frozen=True)
class Intent:
    """A request to transition. Dispatched, never awaited.

    `extras` carries the PURPOSE, as an Android intent does — the world map is entered to set
    sail or to make a remote check, and behaves differently for each, so the purpose is part of
    the request rather than something the activity has to infer.
    """

    name: str
    extras: Mapping[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        if not self.extras:
            return self.name
        inner = ", ".join(f"{k}={v!r}" for k, v in sorted(self.extras.items()))
        return f"{self.name}({inner})"


@dataclass(frozen=True)
class ActivityResult:
    """What an activity hands back. Never a claim about the world having changed."""

    status: str
    observed: Mapping[str, Any] = field(default_factory=dict)
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == FINISHED


class Activity(Protocol):
    """A world the bot can be resumed in and do work in.

    `work` does LOCALIZED perception — reading prices on a market screen it already knows it is
    on — and bounded, goal-directed work. It runs that work to COMPLETION and then hands back;
    it does not return "still working" to be re-entered each tick.

    It must never cause a transition. If it cannot recognise the screen it returns
    UNRECOGNISED and ends immediately, leaving the dispatcher — which can see the whole state
    and the current goal — to decide whether that means recover, re-plan, or give up.
    """

    name: str

    def work(self, goal: Any, state: Any) -> ActivityResult: ...


def _serves_goal(activity: Any, goal: Any) -> bool:
    """May this activity be handed this goal?

    `GOALS` is the goal-side twin of `SERVES`: SERVES says which STATES an activity works in,
    GOALS says which ORDERS it can fill. An activity that declares neither empty GOALS nor a
    matching one is being asked for work it has no way to do.

    Declaring NO goals means "I am in the way of everything" — the lock, the notice, the
    unnameable chromed screen. Those exist to clear a state so the real work can proceed, so
    they run whatever the order is and ignore it (their `work` takes `goal` and drops it).
    """
    if goal is None:
        return True
    goals = getattr(activity, "GOALS", None)
    if not goals:
        return True
    return isinstance(goal, tuple(goals))


# HOW LONG A TRANSITION TAKES TO LAND, when the game moves the player rather than the screen.
#
# Entering a building is a WALK across the port — the tap is issued, the character sets off,
# and for several seconds the screen is still the port overworld. Perceiving into that gap
# sees the port, re-dispatches, and puts a second tap on the queue that lands INSIDE the
# building once the scene loads, on whatever happens to be under it. Live 2026-09-01 at
# Tripoli it tapped 'market' twice, 12 seconds apart; the second landed on the market screen
# and hit nothing only by luck. `navigate_to_building` knew this and waited 60s — that path
# was retired and the patience went with it (user, 2026-09-02: "the bot should wait 20
# seconds before perceive, to allow the player run the distance").
#
# This is NOT the rule `_set_wake_timer` guards against. An ACTIVITY's `checkback_s` describes
# the world as it was before the tick acted, so sleeping on it is sleeping on a stale picture.
# A transition's settle describes the transition ITSELF, which is exactly the thing being
# waited for.
_INTENT_SETTLE_S = {"ENTER_BUILDING": 20.0}

# Intents worth ONE more attempt when the screen has not moved since they were dispatched.
#
# Not every intent: re-issuing OPEN_WORLD_MAP taps the rail again, and the rail TOGGLES — the
# world map opened and re-closed seventeen times over four minutes that way (2026-09-01). An
# intent belongs here only when repeating it is harmless because it is idempotent on the
# screen it acts on.
#
# ENTER_BUILDING is here because its settle has already been waited out by the time an
# unchanged screen is seen, so the walk is over and the tap is simply lost.
#
# EXIT_BUILDING is here, but ONLY FROM INSIDE A BUILDING — see `_RETRY_EXIT_FROM` below.
# Live 2026-09-02 at Lisboa: the first Back left the Sell sub-menu, the second was dropped,
# and with no settle declared nothing retried it. Three unchanged ticks and the mission died
# in the market, 12 seconds after a Back that worked; a manual Back cleared it instantly.
_RETRY_ONCE_IF_UNCHANGED = frozenset({"ENTER_BUILDING", "EXIT_BUILDING"})

# WHERE A SECOND BACK LANDS DECIDES WHETHER THERE MAY BE ONE.
#
# From a building or a sub-menu it lands on the port overworld, and `_NEVER_BACK_FROM` stops
# it going further — so a repeat is recoverable even if the first press had in fact worked.
#
# From a VILLAGE it lands at SEA. `test_depart_village_for_the_tail` records what that cost:
# "the old loop pressed Back up to four times at a screen that never moved... that is how the
# fleet lost the village twice", and at the main menu Back means "Exit Game?", where two
# presses once left the bot one positive tap from quitting. Those paths press ONCE, and this
# change must not quietly undo that.
#
# The retry only fires on an OBSERVED-unchanged screen, so a Back that worked is never
# followed by another — but the blast radius when that observation is wrong is a lost village
# on one side and a re-entered market on the other.
#
# VILLAGE IS HERE NOW (user, 2026-09-04), and what changed is the reading of that cost. A
# second Back at a village lands at SEA, which is only a loss while there is still work in the
# village. EXIT_BUILDING is dispatched when leaving IS the goal — the barter is over and the
# tail leg is next — so "lost the village" and "left the village" are the same event, and the
# guard was protecting nothing at the only moment it fires.
#
# What it cost live 2026-09-04 at San: four rounds committed, 4,455 Bambara Groundnut aboard,
# and one swallowed Back stranded the lot. Back was pressed at 19:11:47 and never again;
# three unchanged ticks later the mission failed with the fleet standing in the village.
#
#     19:12:03  EXIT_BUILDING ... already dispatched — letting it land
#     19:12:12  EXIT_BUILDING ... already dispatched — letting it land
#     19:12:21  NOTHING CHANGED for 3 ticks (state='village')
#
# The old note is still right about the ORIGINAL failure it records: that loop pressed Back
# "up to four times at a screen that never moved". This is one extra press, gated on an
# unchanged observation, and still bounded by the same stall guard — not that loop returning.
_RETRY_EXIT_FROM = ("building", "sub_menu", "village")

# When the family classifier's answer stands on its own. Measured on the frames that
# defeated the label test: 0.9998, 0.958, 0.996 — it is either sure or it is not.
_FAMILY_IS_SURE = 0.90

# How long to let a port finish drawing before looking again, and how many times. The port
# arrived ~4s after the sea frame with none of its UI; 20s is the same allowance a walk to a
# building gets, and three looks is a minute — long past any render, so reaching the limit
# means the cluster is unreadable rather than absent, which is a different problem and must
# not be waited on for ever.
_PORT_DRAW_SETTLE_S = 20.0
_MAX_UNDRAWN_LOOKS = 3
_PORTS = ("port_overworld",)


def _looks_like_a_port(state) -> bool:
    where = getattr(state, "state", None) or getattr(state, "location", None)
    return str(where or "") in _PORTS


# THINGS THAT CHANGE BY THEMSELVES. The status-bar clock, the in-game clock, a countdown —
# each moves with no action from the bot, so leaving them in the screen signature would make
# every screen look "changed" once a minute. That errs toward retrying rather than stalling,
# but it is still a lie about cause and effect: a genuinely stuck intent would earn one free
# re-dispatch a minute forever.
_TICKS_ON_ITS_OWN = re.compile(r"^[\d\s.:,/-]+$|^\d+[dhms]\b|left$|^\d{1,2}[.:]\d{2}")


class Dispatcher:
    """Perceive, run the resumed activity, report up, dispatch the next intent."""

    def __init__(self, *, perceive: Callable[[], Any],
                 activities: Mapping[str, Activity],
                 next_goal: Callable[[Optional[ActivityResult], Any], Any],
                 to_intent: Callable[[Any, Any], Optional[Intent]],
                 dispatch: Callable[[Intent], Any],
                 reorient: Optional[Callable[[Any], Any]] = None,
                 refresh: Optional[Callable[[Any], Any]] = None,
                 unblock: Optional[Callable[[Any], bool]] = None,
                 dialog: Optional[Callable[[Any], Any]] = None) -> None:
        self._perceive = perceive
        self._undrawn_looks = 0          # consecutive looks at a port whose UI has not landed
        self._in_flight_looks = 0        # consecutive unchanged looks at one dispatched intent
        self._activities = dict(activities)
        # THE TASK RUNNER IS TOLD THE STATE; IT MUST NEVER NEED TO UNDERSTAND IT. It decides
        # from task progress — which materials are aboard, which legs are done — never by
        # branching on `sub_menu:purchase` or `port_map`. Those are the dispatcher's
        # vocabulary, and a task runner that reads them takes on knowledge it must then keep
        # in sync with every UI change the game ships. The test of the interface: could this
        # task runner still decide if the UI were replaced wholesale?
        self._next_goal = next_goal      # the TASK RUNNER: result + state -> the next goal
        self._to_intent = to_intent      # goal + state -> the intent that starts an activity
        self._dispatch = dispatch        # the ONLY thing that moves the bot between worlds
        self._reorient = reorient        # lost -> move to a state work can happen in
        self._refresh = refresh          # lost -> drop what the wrong belief cached
        self._unblock = unblock          # clear an obstruction laid OVER the world
        self._dialog = dialog            # frame -> DialogModel | None (injectable for tests)
        self._dialog_looks = 0           # consecutive ticks with a dialog still up
        # WHERE WE WERE, for an `unknown` that is a presentation change and not a move.
        self._standing_in: Optional[str] = None   # state an activity last worked in
        self._standing_looks = 0                  # consecutive unknowns carried on it
        self.goal: Any = None
        self.last: Optional[ActivityResult] = None
        self._fresh: Any = None          # the post-activity look, for the next tick to reuse
        self._wake_at: float = 0.0       # when the activity asked to be looked at again
        self._wake_why: str = ""
        self._in_flight: Any = None      # (intent, state) already dispatched and not yet landed

    @staticmethod
    def _screen_signature(state) -> tuple:
        """What is on the screen, coarsely — the labels, not the pixels.

        Used to tell "my action did nothing" from "my action did something the state label is
        too coarse to show". Falls back to a constant when there is nothing to read, which
        makes the guard behave exactly as it did before.
        """
        frame = getattr(state, "frame", None)
        if frame is None:
            return ()
        # THE WORLD IS THE FAMILY CLASSIFIER'S ANSWER, and it must not be overruled by
        # scattered string matching (user, 2026-09-02). Labels were the whole test, and on a
        # port they include OTHER PLAYERS walking past (`taylorfp`, `mary`, `tay`), the
        # announcement ticker (`season 1 admiral of m`), an investment banner, and OCR jitter
        # on one unchanged string (`lv93_` vs `lv 93`). Measured across the Tripoli entry:
        # two looks at the SAME port shared only 0.50-0.62 of their labels, while port vs
        # market shared 0.08-0.10 — so "changed" fired constantly on a screen nobody had
        # touched, and the guard let a second tap out mid-walk.
        #
        # The CNN had it right on every one of those frames: port_overworld 0.9998, then
        # chromed 0.958 and 0.996. A world change is what it is trained to see, so when it is
        # confident, it decides. Labels stay as the fallback for when it is not — they can
        # still show a panel opening within one world, which the family cannot.
        try:
            from vision.family_classifier import classify_family
            fam = classify_family(frame)
            if fam is not None and getattr(fam, "confidence", 0.0) >= _FAMILY_IS_SURE:
                return ("family", str(getattr(fam, "family", "")))
        except Exception as exc:                   # noqa: BLE001 — fall through to labels
            logger.debug(f"[dispatch] family read failed, using labels: {exc}")
        try:
            from vision.omniparser import parse_fast_cached
            return tuple(sorted({lab for lab in
                                 ((getattr(e, "label", "") or "").strip().lower()
                                  for e in (parse_fast_cached(frame) or []))
                                 if lab and not _TICKS_ON_ITS_OWN.match(lab)}))
        except Exception as exc:
            logger.debug(f"[dispatch] no screen signature: {exc}")
            return ()

    def _port_has_finished_drawing(self) -> bool:
        """Is the port's UI on screen yet? Unknown counts as DRAWN.

        Asked of the frame the tick already holds — never a fresh capture, which would be a
        second look inside one tick and could disagree with the first. If the frame or the
        parse is unavailable this returns True: a check that cannot see must not be the thing
        that stops the mission, and every downstream reader already handles a missing name.
        """
        try:
            from actions.perception import screen
            obs = screen().current_if_valid()
            if obs is None or getattr(obs, "frame", None) is None:
                return True
            from vision.chrome_via_omniparser import port_overworld_is_drawn
            return port_overworld_is_drawn(obs.frame, screen().elements_if_ready())
        except Exception as exc:
            logger.debug(f"[dispatch] could not tell whether the port is drawn: {exc}")
            return True

    def _wait_out_the_wake_timer(self) -> bool:
        """Sleep until the activity asked to be looked at again. Waiting only.

        RETURNS WHETHER IT ACTUALLY SLEPT, because a sleep spends the look that preceded it
        — see `step`, which drops `_fresh` when this returns True.

        THE ACTIVITY STATES A WAKE TIME AND HANDS BACK; IT NEVER SLEEPS (user, 2026-08-31).
        That keeps it passive — it reports `checkback_s` in its observation and the dispatcher
        owns both the waiting and what to do on waking, which may be a DIFFERENT activity
        entirely: arrived at a port, still at sea, or idle-locked. Nothing about the wake
        presumes we are still where we were.

        Two separate jobs, deliberately not merged: the wake timer decides WHEN the next step
        happens, and `step` decides that it wants a fresh look. This briefly owned both, which
        made a fresh look conditional on having waited — and a step that neither waited nor
        acted then re-read a frame it already held.

        Only `sea` supplies `checkback_s` today; every other activity acts and hands back, so
        its next look is driven by its own action rather than by a clock.
        """
        if not self._wake_at:
            return False
        remaining = self._wake_at - _time.monotonic()
        self._wake_at = 0.0
        if remaining <= 0:
            return False
        # Imported here, not at module scope: `run_goal` imports this module, so a top-level
        # import would be a cycle. The jitter matters — a fixed cadence is what the game's
        # anti-cheat looks for.
        from brain.run_goal import _sleep_jittered
        logger.info(f"[dispatch] {self._wake_why} — waiting "
                    f"{remaining / 60:.1f} min before the next look")
        _sleep_jittered(remaining)
        return True

    def _afforded_here(self, intent, where, state):
        """`intent` if this world can start it — otherwise ask the task for other work.

        THE WORLD DECIDES WHAT CAN BE STARTED IN IT (user, 2026-09-01). `to_intent` answers
        "where does this work happen" and, if we are not there, "go there" — with no case for
        "you cannot get there from here". So a market goal at sea becomes ENTER_BUILDING at
        sea, `tap_building_entry` refuses because there is no building list, and the refusal
        concludes nothing, so the next tick asks the identical question. Dispatch, refuse,
        repeat, stall — the phrase `tap_world_map_control` already uses for its own version of
        this.

        It has happened in three different worlds now, and each was patched by name where it
        surfaced: OPEN_WORLD_MAP at a village, OPEN_WORLD_MAP from a sub-menu, and on
        2026-09-01 ENTER_BUILDING at sea, which cycled the minimap's four tab icons for
        minutes because they look like the port building list's tabs. One `if where ==` per
        world, added as each one bites, is what the declarations replace.

        A REFUSAL IS NOT AN ANSWER, so it is not the end of the step. The task runner is told
        — through the same `result` channel it already reads, so its signature does not grow
        — and asked once more. It is the only thing that knows a different order might do:
        at sea the honest next order is to arrive somewhere, not to shop. If what comes back
        is also unservable the step ends with the mismatch LOGGED AS A DEFECT rather than
        silently skipped, because a work order nobody can serve is a bug in the plan and it
        should read like one.

        Exactly one re-ask. Two would be a loop in the place this method exists to unloop.
        """
        # A SCREEN THAT COVERS THE WORLD IS NOT A WORLD, AND MUST NOT BE JUDGED AS ONE.
        # A full-screen notice, the idle lock and an unnameable chromed screen are drawn OVER
        # the place the fleet is in; asking whether one "affords ENTER_BUILDING" is asking
        # about the wrong thing, and the answer is always no. What they need is clearing, and
        # the activity that does it is about to run.
        #
        # Live 2026-09-01 a notice appeared mid-departure and this called it a DEFECT — then
        # the very next line was "[transient] tapping the notice away". Worse than the noise,
        # the re-ask made `SailRunner` spend one of its two departure attempts on a tick where
        # nothing could have worked: "departing for 'Barcelona' (2/2)".
        #
        # Derived from `CLEARS_SCREEN`, the marker these activities already carry, so no new
        # list of screens has to be kept in step with them.
        if self._is_a_covering_screen(where):
            logger.info(f"[dispatch] {where!r} is covering the world — not judging what it "
                        f"affords; {intent.name} waits for it to clear")
            return None

        can = self._affordances(where)
        if can is None or intent.name in can:
            return intent

        # DECLINING IS NOT ROUTING. Before handing the problem back, ask whether a world that
        # CAN start this is reachable from here — the harbour cannot open a building list, but
        # stepping out to the overworld and in again is an ordinary two-hop route, and the
        # capability check turned that into a hard stop when it was added (2026-09-01).
        #
        # Only the FIRST hop, because the world after it is observed rather than assumed: the
        # next look says whether it landed and the route is recomputed from there.
        hop = self._first_hop_toward(intent.name, where)
        if hop is not None:
            logger.info(f"[dispatch] {where!r} cannot start {intent.name}, but {hop} leads "
                        f"toward somewhere that can — routing")
            return Intent(hop, dict(intent.extras or {}))

        logger.warning(f"[dispatch] {where!r} cannot start {intent.name} "
                       f"(it affords {sorted(can) or 'nothing'}) — no route from here, "
                       f"asking the task for work this world can actually do")
        refused = ActivityResult(
            BLOCKED,
            {"unservable": str(self.goal), "world": where, "needed": intent.name,
             "afforded": sorted(can)},
            detail=f"{where} cannot start {intent.name}")
        self.last = refused
        self.goal = self._next_goal(refused, state)
        if self.goal is None:
            return None
        intent = self._to_intent(self.goal, state)
        if intent is None or intent.name in can:
            return intent

        # A WORK ORDER NOBODY HERE CAN SERVE IS A DEFECT, not a step to skip quietly.
        logger.error(f"[dispatch] DEFECT: {self.goal} needs {intent.name} and {where!r} "
                     f"affords {sorted(can) or 'nothing'} — nothing dispatched. The task "
                     f"asked twice for work this world cannot do.")
        return None

    def _first_hop_toward(self, intent_name, where):
        """The transition that starts a route to a world able to start `intent_name`."""
        from brain.run_goal import affordances, first_hop_toward
        targets = {w for w in self._activities
                   if intent_name in (affordances(w, self._activities) or ())}
        return first_hop_toward(where, targets, self._activities)

    def _is_a_covering_screen(self, where) -> bool:
        """Is this a screen drawn OVER the world rather than a world of its own?"""
        found = self._activities.get(where)
        served = ([] if found is None
                  else list(found) if isinstance(found, (list, tuple)) else [found])
        return any(getattr(a, "CLEARS_SCREEN", False) for a in served)

    def _affordances(self, where):
        """Which intents `where` can start, or None when nobody declared."""
        from brain.run_goal import affordances
        return affordances(where, self._activities)

    def _set_wake_timer(self, result, intent) -> None:
        """Record when the activity wants the next look — unless this tick ACTED.

        NEVER SLEEP ON A TICK THAT ACTED. Live 2026-08-29: the sea asked for 7.5 minutes
        mid-voyage, and by the end of that same tick the fleet had reached Tripoli, been handed
        'hold Candle (~934)' and tapped into the market — then slept 7.5 minutes on the
        market's doorstep. The pause describes a world the tick's own second look has already
        left behind.
        """
        self._wake_at = 0.0
        if intent is not None:
            # ...unless the TRANSITION itself takes time to land — see `_INTENT_SETTLE_S`.
            settle = _INTENT_SETTLE_S.get(getattr(intent, "name", ""))
            if settle:
                self._wake_at = _time.monotonic() + float(settle)
                self._wake_why = (f"{intent.name} moves the player, not the screen — "
                                  f"waiting {settle:.0f}s for the walk")
            return
        if getattr(result, "status", None) != WORKING:
            return
        wait = (getattr(result, "observed", None) or {}).get("checkback_s")
        if not wait:
            return
        self._wake_at = _time.monotonic() + float(wait)
        self._wake_why = str(getattr(result, "detail", "") or "the activity asked to wait")

    def step(self) -> dict:
        """ONE HAND-BACK: perceive, route, let an activity do one thing, return what happened.

        Named `tick` until 2026-08-31, and that name said something untrue. A tick implies a
        periodic sample of the world; this is not periodic and does not sample. It runs when
        the previous step returned, and in a port or a village nothing changes unless WE
        change it — so a step there advances the work rather than checking on it.

        The only genuinely periodic behaviour left is a wake timer, which an activity asks for
        by reporting `checkback_s` and the dispatcher waits out (see `_set_wake_timer` /
        `_wait_out_the_wake_timer`). Today only `sea` ever asks, because only at sea does the
        world move with nobody acting.

        The name also collided with a DIFFERENT protocol: `HugShoreGoal.tick()` and friends
        return a `TickResult` and are driven at 0.15-0.40 s by their own control loop. Those
        really are ticks. This is not one.
        """
        # A SLEEP SPENDS THE LOOK THAT PRECEDED IT, exactly as an intent does. `_fresh` is
        # captured at the END of the previous step — BEFORE this wait — so reusing it here
        # decides a 9-minute-old world, and then sets the NEXT sleep from it too.
        #
        # Live 2026-09-04 sailing to San Village, four readings:
        #     18:28:50  eta=11d -> 0.5 min      18:29:59  eta=6d -> 9.0 min
        #     18:29:31  eta=7d  -> 0.3 min      18:38:17  eta=6d -> 9.0 min
        # The ETA did not move across 8.3 minutes of sailing, because the fourth reading WAS
        # the third: the frame behind it was captured at 18:30:00. The fleet is always one
        # whole sleep behind, so it notices arrival a sleep late and re-books the same wait
        # from an ETA that can never fall.
        #
        # At sea this never self-corrected: `_fresh` is only dropped after an INTENT is
        # dispatched, and a sea tick dispatches none. The rule is the architecture's own —
        # never act on a reading the world has already contradicted — and nine minutes of
        # sailing contradicts one.
        if self._wait_out_the_wake_timer():
            self._fresh = None

        # EVERY STEP TAKES A FRESH LOOK. The dispatcher regaining control IS the caller that
        # expects the world to have changed (user, 2026-08-31) — and that is every step, not
        # only the ones after a wait. Between two steps the game can arrive, raise a notice,
        # pop a reward or idle-lock, none of which we caused.
        #
        # Narrowing this to "after a wait" broke a live run within minutes (2026-08-31): the
        # in-flight guard declined to re-dispatch ENTER_BUILDING because "the screen has not
        # changed", so NO action was taken, so nothing invalidated, so the next look returned
        # the same held frame — and "unchanged" became self-fulfilling. A daily-news popup sat
        # on screen that the bot could not see, and the loop spun.
        #
        # This is not the repository guessing about the world. It is the dispatcher stating an
        # expectation it genuinely holds, at the one moment it holds it. And it costs nothing
        # extra: `perceive` reads THROUGH the repository now, so a step captures once and
        # every reader in that step shares it.
        try:
            from actions.perception import screen
            screen().expect_changed("the dispatcher is taking a fresh look")
        except Exception as exc:
            logger.debug(f"[dispatch] could not ask for a fresh observation: {exc}")

        # The previous tick already looked at the world AFTER its activity finished; that look
        # is this tick's starting point. Perceiving again here would pay twice for one truth.
        state, self._fresh = (self._fresh, None) if self._fresh is not None else (self._perceive(), None)

        # AN OBSTRUCTION IS NOT A WORLD. The daily news, a promo, an announcement — each is
        # a film laid OVER a world the bot is still in, and none is somewhere work can
        # happen. They are cleared here, before an activity is resolved, because they can
        # appear over ANY world: giving them to an activity means giving them to every
        # activity, and clearing one changes the screen, which is the dispatcher's business.
        #
        # NOT the idle lock, which this comment used to include (corrected 2026-08-26). The
        # lock replaces the ENTIRE screen — nothing of the previous world survives — so it is
        # a genuine STATE, with a known exit action and an UNKNOWN destination, and it is
        # dispatched on like any other. The test is whether the world underneath is still
        # there. See docs/architecture_DRAFT.md, "Two kinds of unknown".
        #
        # Live 2026-08-26 at Svear Village, the game dropped into "Slide up to unlock" while
        # the barter panel was opening. Perception NAMED it correctly on every look —
        # `learned_on_standby_at_sea_slide_up_to_unlock: 1/1 signals matched` — and nothing
        # acted on the name, so the mission re-perceived a screen it could not act through
        # for 34 minutes across two attempts, then aborted with the materials aboard.
        #
        # Note what that screen SAID: "On Standby at Sea", server "Atlantic Ocean", while the
        # fleet stood in a village. Routing it by its wording would have handed a village
        # problem to the sea. It is not a state and must not be modelled as one.
        # A PORT THAT IS STILL DRAWING IS NOT A PORT TO DECIDE ON (user, 2026-09-02).
        #
        # The scene renders before the UI does. For a few seconds after the sea a frame is
        # unmistakably a port to the family classifier and carries none of the things a port
        # is read FOR — no name banner, no ☰, no right-edge cluster. Deciding there reads a
        # port with no name, and every consumer that then falls back to something remembered
        # is reading a stale world.
        #
        # Live 2026-09-02, four seconds after leaving the sea for Faro: classified
        # port_overworld at 1.00 with `port=None`, and the voyage's arrival check compared
        # the destination against a name from somewhere other than that frame, concluded the
        # fleet was still at Lisboa, and failed the mission. The fleet was standing in Faro.
        #
        # Waiting is the dispatcher's job, not perception's: perception reports what it sees
        # and this decides that what it saw is not yet worth acting on. Bounded, because a
        # port whose cluster never appears is a reading problem and looping on it would
        # strand the mission — after that we act on what we have and say so.
        if _looks_like_a_port(state) and self._undrawn_looks < _MAX_UNDRAWN_LOOKS:
            if not self._port_has_finished_drawing():
                self._undrawn_looks += 1
                logger.info(
                    f"[dispatch] the port is still drawing (no right-edge cluster) — "
                    f"waiting {_PORT_DRAW_SETTLE_S:.0f}s rather than deciding on it "
                    f"({self._undrawn_looks}/{_MAX_UNDRAWN_LOOKS})")
                self._wake_at = _time.monotonic() + _PORT_DRAW_SETTLE_S
                return {"did": "waited for the port to finish drawing"}
            self._undrawn_looks = 0
        elif self._undrawn_looks >= _MAX_UNDRAWN_LOOKS:
            logger.warning(
                "[dispatch] the port's right-edge cluster never appeared after "
                f"{_MAX_UNDRAWN_LOOKS} looks — acting on the screen as read. If a port name "
                "is missing downstream, this is why.")
            self._undrawn_looks = 0

        if self._unblock is not None and self._unblock(state):
            logger.info("[dispatch] cleared an obstruction — re-perceiving")
            state = self._perceive()

        # WHAT THE MOVE INVALIDATED, dropped before anyone can read it.
        #
        # Data has an OWNER and dies with it (CLAUDE.md). This is the only layer that sees
        # both the previous world and this one, so it is the only place that can know a
        # boundary was crossed — and leaving a place ends its buildings and their panels
        # without anyone enumerating them.
        #
        # Live 2026-08-26 is the cost of not doing it: a goal held "could not depart" across
        # a departure that had worked, re-opened the world map mid-voyage, and sailed back to
        # the port it had just left. The world had already answered; nothing dropped the
        # answer it contradicted.
        owned_state.observe(state)

        where = getattr(state, "state", None) or getattr(state, "location", None)

        # A DIALOG IS A WINDOW, AND THE SCREEN BEHIND IT HAS LOST FOCUS (user, 2026-09-03).
        # See `docs/dialogs_are_windows.md`. The game reproduces Android's mechanics: the
        # dialog is a separate window with FLAG_DIM_BEHIND (a measured x1.98 scrim), the
        # activity behind keeps its state but stops receiving input, and only the activity
        # that raised the dialog knows what its buttons mean. So the OBSERVATION is made once,
        # here, and the INTERPRETATION is handed down.
        #
        # BEFORE THE ACTIVITY IS PICKED, and that ordering is the whole point. A blocking
        # dialog is precisely what stops the screen being recognised — live 2026-09-03 a
        # `Replenish Stock?` prompt classified as `unknown`, `_pick` returned None, and the
        # dispatcher handed back at "no activity for state 'unknown'" WITHOUT EVER LOOKING FOR
        # A DIALOG, because this check used to sit after the pick. It ran only when the screen
        # was already legible, which is when a dialog is least in the way. The same shape as
        # the KIND_NONE crash: sound in the tested path, unreachable in the live one.
        #
        # An unnameable screen is not a reason to skip the dialog — it is the strongest reason
        # to look for one. `activity` may be None here; the activity gets first refusal when
        # there is one, and the game rules answer either way.
        activity = self._pick(where, self.goal)

        dialog = self._dialog_on(state)
        if dialog is not None:
            self._dialog_looks += 1
            handled = self._offer_dialog(activity, dialog, state)
            if handled is not None:
                return handled
        else:
            self._dialog_looks = 0

        if activity is None:
            # AN ACTIVITY BEHIND A DIALOG HAS NOT GONE ANYWHERE (user, 2026-09-04).
            #
            # The game WITHDRAWS a chromed screen's furniture while a modal is up — measured
            # on frame_0207 of trace_barter_cmd_2026-09-03T22-23-54, the back arrow, title and
            # help icon are simply not drawn (region p99 11.6 against 255.0 once the dialog
            # was answered). So `has_back_arrow=False` is a CORRECT reading of a bar that is
            # not there, the chromed test cannot fire, and a market reads as `unknown` — which
            # is an honest answer to "what screen is this", because the screen has stopped
            # presenting itself.
            #
            # But it is the wrong answer to "where am I". Android's Activity stays RESUMED
            # behind its dialog; only its window focus goes. Ours is the same: nothing moved,
            # the presentation changed. So when we did not ASK to leave, carry the activity we
            # were standing in rather than handing back and re-deriving.
            #
            # GATED ON NOT HAVING ASKED TO LEAVE, which is what keeps this from becoming the
            # stored conclusion the architecture warns about. The moment an intent is
            # dispatched that moves us — EXIT_BUILDING, OPEN_WORLD_MAP, ENTER_BUILDING — the
            # memory is dropped in `_advance` and an `unknown` means what it always did.
            # Bounded too: a few ticks of carrying it, then hand back. An `unknown` that
            # outlasts a dialog is not a dialog.
            carried = self._carry_on_standing(where)
            if carried is not None:
                activity = carried
            else:
                # Not a world we can work in — a transition screen, or something unmodelled.
                # This is NOT an error and NOT recovered from here; the task runner is told and
                # decides. `loading` lands here by design: nothing is resumed, so nothing can work.
                logger.info(f"[dispatch] no activity for state {where!r} — asking for a goal")
                result = ActivityResult(UNRECOGNISED, observed={"state": where},
                                        detail=f"no activity for {where!r}")
                return self._advance(result, state)

        if self.goal is None:
            self.goal = self._next_goal(self.last, state)
            if self.goal is None:
                logger.info("[dispatch] the task runner has nothing to do")
                return {"state": where, "activity": activity.name, "goal": None,
                        "result": None, "intent": None}

        # TWO LEVELS OF DISPATCH: the ORDER decides which activity, the STATE decides whether
        # we are there yet. Resolving an activity from the state alone and handing it the goal
        # regardless is one level, and it puts orders in front of activities that cannot fill
        # them. Live 2026-08-27: `Hold(orders={'Iron': 506, 'Matchlock Gun': 253})` was handed
        # to `transient` — the arrival cinematic — which has one action and cannot buy. It
        # reported lost three times, the goal ran out of ticks, and the mission moved on to
        # the village having never looked at the Matchlock shelf.
        #
        # An activity that declares GOALS serves only those. One that declares none is a
        # state-CLEARING activity (the idle lock, a full-screen notice, an unnameable chromed
        # screen): it is in the way of every goal and must run whatever the order is, which is
        # exactly why it declares none.
        if not _serves_goal(activity, self.goal):
            logger.info(f"[dispatch] {activity.name} does not serve {self.goal!r} — handing "
                        "back so a transition can be dispatched toward one that does")
            result = ActivityResult(UNRECOGNISED, observed={"state": where},
                                    detail=f"{activity.name} does not serve {self.goal!r}")
            return self._advance(result, state)

        logger.info(f"[dispatch] {activity.name} <- {self.goal!r}")
        if where not in (None, "unknown"):
            # Only a screen we could NAME establishes where we are standing. Remembering an
            # unknown would let the memory bootstrap itself off its own guess.
            self._standing_in, self._standing_looks = where, 0
        result = activity.work(self.goal, state)
        logger.info(f"[dispatch] {activity.name} -> {result.status} {dict(result.observed)}")

        # A STATE-CLEARING ACTIVITY FINISHES ITS ACTION, NOT THE ORDER. The lock's swipe, the
        # notice's tap and the chromed screen's exit all report FINISHED when they have done
        # the one thing they do — which says nothing about whether the goal was served. The
        # task runner reads FINISHED as "this goal is done", so passing it up retires an order
        # nobody filled.
        #
        # Live 2026-08-27, arriving at Barcelona owing 95 Matchlock Gun:
        #     transient <- Hold(orders={'Iron': 506, 'Matchlock Gun': 253})
        #     transient -> finished {'gesture': 'tap'}      <- dismissed a notice
        #     ... next goal is the TRIM; the buy was never attempted
        # The purchase was reported complete by an activity whose entire repertoire is one
        # tap, and the mission sailed to the village still short.
        #
        # Reported as UNRECOGNISED instead: the screen was cleared, nobody knows where that
        # leaves us, so re-perceive and ask the task runner again — which returns the SAME
        # goal, still unfilled.
        #
        # Gated on an EXPLICIT `CLEARS_SCREEN`, never on "declares no GOALS". An absent
        # declaration is not a statement about what an activity does, and reading it as one
        # silently reclassified every activity that had not got round to declaring goals.
        if result.status == FINISHED and getattr(activity, "CLEARS_SCREEN", False):
            logger.info(f"[dispatch] {activity.name} cleared the screen; that is not "
                        f"{self.goal!r} being done — re-perceiving and keeping the goal")
            result = ActivityResult(UNRECOGNISED, result.observed,
                                    detail=f"{activity.name} cleared the screen "
                                           f"({result.detail}); the goal is untouched")
        return self._advance(result, state)

    # A dialog that survives this many safe exits is a fact to report, not something to
    # grind at. Each look is a fresh tick, so the re-check is the LOOP verifying the tap —
    # never a sub-loop waiting in place for its own effect.
    _MAX_DIALOG_LOOKS = 3

    def _dialog_on(self, state) -> Any:
        """The dialog covering this tick's screen, or None. Never raises."""
        if self._dialog is not None:
            return self._dialog(state)
        frame = getattr(state, "frame", None)
        if frame is None:
            return None
        try:
            from vision.omniparser import parse_fast_cached
            from vision.region_detectors.dialog import detect_dialog
            return detect_dialog(list(parse_fast_cached(frame)),
                                 frame.width, frame.height, frame=frame)
        except Exception as exc:                  # noqa: BLE001 — blind to dialogs, not broken
            logger.debug(f"[dispatch] could not look for a dialog: {exc}")
            return None

    def _dimmed(self, state: Any) -> bool:
        """Is something dimming the screen — i.e. is a window actually over it?

        Unknown reads as DIMMED, which is the safe direction here: it leaves the long-standing
        behaviour in place and only the confident "nothing is dimmed" refuses a tap.
        """
        frame = getattr(state, "frame", None)
        if frame is None:
            return True
        try:
            from vision.overlay import CLEAR, scrim_state
            return scrim_state(frame) != CLEAR
        except Exception as exc:              # noqa: BLE001 — a poorer read, not a broken one
            logger.debug(f"[dispatch] could not read the scrim: {exc}")
            return True

    def _words_inside(self, dialog: Any, state: Any) -> list:
        """Every label the dialog's own bounds contain — the rules decide, we OBSERVE.

        `body_text` is the detector's INTERPRETATION: the text between the title bar and the
        topmost action button, taken from the cluster it thinks is the card. When the card is
        not shaped like a card that reading loses the very words a rule needs.

        Live 2026-09-06, the Attempt Negotiation screen. It is not a centred modal at all —
        the mate's portrait and "Want me to try negotiating?" sit LEFT, the three choices
        RIGHT — so the detector bounded it at (33,121)-(2239,1080), essentially the frame, and
        `body_text` came back `['Purchase', '3,330/4,952', '162']`: page furniture, with
        'Attempt Negotiation' and 'Remaining negotiation attempts' both present on screen and
        both dropped. `game_rules` was then asked to rule on a card it could not read, said
        so honestly, and bootstrap died at step 1 on a screen left over from a previous run.

        THE COST OF WIDENING IT. On a well-bounded dialog this is exactly the card's text. On
        a mis-bounded one it is closer to the whole frame, so a rule could in principle match
        something behind the card. That is bounded by keeping rule phrase-sets narrow and
        specific — `DialogRule` requires ALL phrases — and it is the better failure: a rule
        that occasionally sees too much beats a decision layer that is handed nothing and
        wedges the run. Deciding stays in `game_rules`; this only widens what it is shown.
        """
        bbox = getattr(dialog, "bbox", None)
        frame = getattr(state, "frame", None)
        if bbox is None or frame is None:
            return []
        x1, y1, x2, y2 = bbox
        try:
            from vision.omniparser import parse_fast_cached
            return [str(e.label).strip() for e in parse_fast_cached(frame)
                    if (e.label or "").strip()
                    and x1 <= e.cx <= x2 and y1 <= e.cy <= y2]
        except Exception as exc:              # noqa: BLE001 — a poorer read, not a broken one
            logger.debug(f"[dispatch] could not read inside the dialog: {exc}")
            return []

    def _offer_dialog(self, activity: Activity, dialog: Any, state: Any):
        """Give the activity first refusal on the dialog; fall back to the safe exit.

        Returns a step record when the dialog was dealt with, or None to let the activity's
        normal work proceed (which it may, if the activity says the dialog is not in its way).
        """
        kind = dialog.kind() if hasattr(dialog, "kind") else "unknown"
        who = getattr(activity, "name", None) or "no activity"
        handler = getattr(activity, "on_dialog", None) if activity is not None else None
        if handler is not None:
            # THE ACTIVITY MUST SEE THIS TICK'S SCREEN, NOT THE LAST ONE. `on_dialog` runs
            # BEFORE `work()`, which is where an activity normally receives the tick's frame,
            # so without this it classifies whatever the previous tick left behind.
            #
            # Live 2026-09-06 at Tripoli: the market's `on_dialog` read the PURCHASE PAGE from
            # the previous tick, concluded "a page is not a dialog", and returned None without
            # a word — so its own negotiation handler never ran, the card fell through to
            # `game_rules`, and the leg failed with "a confirmation dialog will not close".
            # The market classifies that frame as `negotiation` correctly when it is given it.
            seen = getattr(activity, "on_tick_frame", None)
            if seen is not None:
                seen(getattr(state, "frame", None))
            result = handler(dialog, self.goal)
            if result is not None and result.status != UNRECOGNISED:
                logger.info(f"[dispatch] {who} answered the {kind} dialog -> "
                            f"{result.status} {dict(result.observed)}")
                return self._advance(result, state)
            logger.info(f"[dispatch] {who} does not own this {kind} dialog")

        # NOTHING OWNS IT — TAKE THE POSITIVE OPTION, WHICH IS WHAT MOVES THE GAME ON.
        #
        # This read the other way round for about an hour, on an over-stretched analogy to
        # Android: Back fires onCancel, so the system never presses your positive button.
        # That rule protects a HUMAN's intent from the framework. Here the bot IS the user,
        # there is no intent to usurp, and cancelling is not neutral — it is refusing to play.
        #
        # The San Village frame is the argument. Cancel returns to `Insufficient Empty Space`,
        # whose `Receive` cannot succeed with a full hold, which raises the same Notice: a
        # loop with no exit, because a VILLAGE has no market to free space in. And the goods
        # are lost either way — "Unclaimed trade goods will be discarded" is what OK
        # ACKNOWLEDGES, not what it causes. The run had already taken that trade three times
        # in the same barter, discarding ~2,000 units to land 4,279 (user, 2026-09-03).
        #
        # THE DECISION IS NOT MADE HERE. `game_rules.answer_dialog` is the one layer that
        # knows the game: named rules first, then the default of the positive option, and a
        # refusal for anything spending RED GEMS, which are real money. Its docstring says
        # exactly why this must not be re-decided in the dispatcher — "the moment IT defaults,
        # this layer is decorative and the decision has silently moved back down to UI
        # mechanics". Which is what the version this replaces had done.
        # BOUNDED. A dialog that survives this many answers is a fact to report, not
        # something to grind at — and each look is a fresh tick, so this IS the verification
        # of the previous tap. Live 2026-09-03 the fourth Ok was swallowed and nothing ever
        # re-checked; now the next tick finds the dialog still up and presses again.
        if self._dialog_looks > self._MAX_DIALOG_LOOKS:
            logger.warning(f"[dispatch] a {kind} dialog is still up after "
                           f"{self._MAX_DIALOG_LOOKS} answers — reporting rather than "
                           "pressing it again")
            result = ActivityResult(BLOCKED, {"dialog": kind, "answers": self._dialog_looks},
                                    detail=f"a {kind} dialog will not close")
            return self._advance(result, state)

        # NO OPTIONS MEANS NO DECISION TO MAKE. An informational dialog offers only its
        # close X — `Gear Info` on 2026-09-04 was exactly this — and `answer_dialog` rightly
        # returns None when handed an empty list, which would report BLOCKED on a dialog that
        # has an obvious, harmless exit. Closing something that commits nothing is not the
        # system pressing a positive button; there is no positive button to press.
        options = [a.label for a in dialog.actions]
        close = getattr(dialog, "close_button", None)
        # THE X MAY NOT BE BOTH THE EVIDENCE AND THE TARGET. `detect_dialog` rests on two
        # anchors — a close X, and a row of action buttons — and reports which fired. When
        # `close` is the ONLY one, the sole reason to believe a dialog is open is the very
        # thing this branch would tap. That is circular, and it is how a tap gets spent on a
        # screen with no dialog at all.
        #
        # Live 2026-09-06 on the Seville leg, this tapped the bare WORLD MAP. The detector
        # returned bbox (973,296)-(2246,1046), `actions=[]`, `anchors_fired=('close',)`, and
        # its "close button" (1335,359)-(1413,414) was the '108 108' trade-value badge beside
        # the Montpellier label. The tap went in at (1374,386) — which on a world map OPENS a
        # port's Location Info. Marseille's. The world-map activity then found a panel for the
        # wrong place, rightly refused to sail from it, and the leg died two attempts later.
        # Every later symptom came from this one tap.
        #
        # A TITLE BAR WOULD NOT HAVE CAUGHT IT, which is worth recording because it was the
        # first fix tried: `_find_title_bar` runs over the cluster AFTER the anchors and
        # happily returned `TitleBar(text='Montpellierlle')` — the map's own port label. Only
        # the anchor set distinguishes the two cases.
        #
        # `Gear Info` — the informational dialog this branch was written for — is unaffected:
        # it is a real card whose X sits in real chrome, and nothing about it changes here.
        # What is refused is acting on ONE weak geometric anchor; the screen still reaches the
        # activity and the state classifier, which is where a real dialog gets recognised.
        # AND THE SCRIM IS WHAT SETTLES IT. A dialog is a WINDOW: it dims what it covers
        # (`docs/dialogs_are_windows.md`, measured x1.98), and `vision.overlay.scrim_state`
        # already reads that. So a lone close-X anchor is refused only when NOTHING is dimmed
        # — no scrim, no window, nothing to close. Measured on the two frames:
        #
        #     world map, no dialog at all   anchors=('close',)  scrim=clear
        #     a real card after a purchase  anchors=('close',)  scrim=scrim
        #
        # Refusing on the anchor alone was too broad and cost a run of its own: at Barcelona
        # a genuine card sat at (813,329)-(1587,754) with only an X, nothing closed it, and
        # the task stopped with "NOTHING CHANGED for 3 ticks".
        anchors = tuple(getattr(dialog, "anchors_fired", ()) or ())
        if not options and close is not None and anchors == ("close",) and not self._dimmed(state):
            logger.info(f"[dispatch] this {kind} rests on a close-X alone and nothing on "
                        "screen is dimmed — the X is the only evidence for it AND the thing "
                        "we would tap, so not tapping it")
            return None
        if not options and close is not None:
            x1, y1, x2, y2 = close
            from actions import ui
            logger.info(f"[dispatch] the {kind} dialog offers no buttons — closing it via its "
                        f"X @ ({(x1 + x2) // 2},{(y1 + y2) // 2}) "
                        f"({self._dialog_looks}/{self._MAX_DIALOG_LOOKS})")
            ui.tap_at((x1 + x2) // 2, (y1 + y2) // 2, why=f"close an unowned {kind} dialog")
            return {"did": f"closed a {kind} dialog with no buttons"}

        from brain.game_rules import answer_dialog
        text = list(getattr(dialog, "body_text", ()) or ())
        if getattr(dialog, "title_bar", None) is not None and dialog.title_bar.text:
            text.append(dialog.title_bar.text)
        text.extend(self._words_inside(dialog, state))
        choice = answer_dialog(options, text)
        if choice is None:
            logger.warning(f"[dispatch] nothing owns this {kind} dialog and the game rules "
                           f"will not answer it (options={options}) — reporting rather than "
                           "pressing something at random")
            result = ActivityResult(BLOCKED, {"dialog": kind, "options": options},
                                    detail=f"a {kind} dialog nobody will answer")
            return self._advance(result, state)

        target = next((a.bbox for a in dialog.actions if a.label == choice), None)
        from actions import ui
        if target is None:
            logger.warning(f"[dispatch] {choice!r} is not on screen after all — not guessing")
            return None
        x1, y1, x2, y2 = target
        logger.info(f"[dispatch] nothing owns the {kind} dialog — the game rules say "
                    f"{choice!r} ({self._dialog_looks}/{self._MAX_DIALOG_LOOKS})")
        ui.tap_at((x1 + x2) // 2, (y1 + y2) // 2, why=f"{choice} on an unowned {kind} dialog")
        return {"did": f"answered a {kind} dialog with {choice!r}"}

    # How many consecutive `unknown` ticks may be carried on a remembered activity. A dialog
    # is answered in one or two; an unknown that outlasts that is not a dialog, and pretending
    # we know where we are stops being continuity and becomes a stored conclusion.
    _MAX_STANDING_LOOKS = 3

    # Intents that MOVE us. Once one is dispatched, where we were standing is history and an
    # `unknown` means what it always did.
    _LEAVING_INTENTS = frozenset({
        "EXIT_BUILDING", "ENTER_BUILDING", "OPEN_WORLD_MAP", "CLOSE_WORLD_MAP",
    })

    def _carry_on_standing(self, where: Any) -> Optional[Activity]:
        """The activity we were standing in, when `where` is an unreadable screen and nothing
        we did was meant to leave it. None means hand back, as before."""
        if where not in (None, "unknown"):
            return None
        if self._standing_in is None:
            return None
        activity = self._activities.get(self._standing_in)
        if activity is None:
            self._standing_in = None
            return None
        self._standing_looks += 1
        if self._standing_looks > self._MAX_STANDING_LOOKS:
            logger.info(f"[dispatch] {where!r} has outlasted {self._MAX_STANDING_LOOKS} looks "
                        f"— no longer assuming we are still in {self._standing_in!r}")
            self._standing_in = None
            self._standing_looks = 0
            return None
        logger.info(f"[dispatch] the screen is {where!r}, but nothing we did was meant to "
                    f"leave {self._standing_in!r} — carrying it "
                    f"({self._standing_looks}/{self._MAX_STANDING_LOOKS}). The game hides a "
                    "chromed screen's furniture behind a modal; that is a change of "
                    "presentation, not of place.")
        return activity

    def _pick(self, where: Any, goal: Any) -> Optional[Activity]:
        """The activity for this state that can serve this goal.

        A STATE MAY HAVE MORE THAN ONE ACTIVITY. `village` is served by `VillageActivity`
        for a Barter and by `AshoreActivity` for an ArriveAshore — the same screen, two
        different jobs — and registering one keyed only by state silently overwrote the
        other (found 2026-08-28: AshoreActivity clobbered VillageActivity because it was
        registered later).

        This is the intent-filter resolution the design already describes: SERVES filters on
        the STATE, GOALS on the ORDER, and the pair identifies the handler. An activity that
        declares no GOALS serves anything, and is preferred LAST so a specific filter always
        wins over a general one.
        """
        found = self._activities.get(where)
        if found is None:
            return None
        candidates = list(found) if isinstance(found, (list, tuple)) else [found]
        # NO SHORT-CIRCUIT FOR A SINGLE CANDIDATE. Skipping the filter when only one activity
        # is registered would resolve the bare port overworld to `AshoreActivity` for ANY
        # goal, when it serves only `ArriveAshore` — and the port's whole normal behaviour is
        # to have NO activity so the dispatcher asks the task runner for a goal and
        # dispatches an intent. Returning None here is what preserves that.
        general = None
        for cand in candidates:
            goals = getattr(cand, "GOALS", None)
            if not goals:
                general = general or cand
            elif goal is not None and isinstance(goal, tuple(goals)):
                return cand
        return general

    def _regain_bearings(self, state: Any) -> Any:
        """An activity got lost. Perceive, transition, refresh — then the caller asks the task
        runner. In that order.

        Being lost is not something the activity could have fixed: re-perceiving IN PLACE tells
        you where you are, it cannot change where you are (memory: lost-means-reorient-not-
        reread). So it ends, and the layer that may cause a transition does the recovering.

        The refresh is the step that is easy to leave out. Getting lost usually means a belief
        was ALREADY wrong — the bot thought it was in the market and it was not — so whatever
        was cached on that belief is suspect. Recovering position without dropping the stale
        belief just re-enters the same mistake from a tidier starting point.
        """
        state = self._perceive()
        # NOTE THERE IS NO ESCAPE HOOK HERE. A chromed screen the cascade cannot name is a
        # STATE with an activity that serves it (`unrecognized_chromed_screen`), so it is
        # reached by the ordinary route — resume the activity, it takes one exit, it
        # finishes, the next tick perceives. Recovery is not a special path.
        if self._reorient is not None:
            moved = self._reorient(state)
            if moved is not None:
                state = moved
        if self._refresh is not None:
            self._refresh(state)
        return state

    def _advance(self, result: ActivityResult, state: Any) -> dict:
        """Re-perceive, hand the result up, take the next goal, turn it into an intent.

        RE-PERCEIVING IS NOT OPTIONAL AND NOT SPECIAL-CASED (user, 2026-08-26). An activity
        finishing means the world may be somewhere else, and the task runner must be asked
        about where the bot IS, not where it was when the activity started.

        The idle lock makes this obvious. Its entire repertoire is one action — swipe up — and
        when that returns, the activity has FINISHED and nobody knows where it landed. But
        nothing about that is lock-specific: re-perceive, then consult the task runner, are the
        two general steps after EVERY activity, which is why the lock needs no wiring into any
        path that might encounter it. The barter code never has to learn what a lock is.

        Asking with the pre-activity state was the bug this replaced: the task runner would
        have been told "you are at the idle lock" about a bot that had just woken into a
        village.
        """
        self.last = result
        if result.status == UNRECOGNISED:
            logger.info("[dispatch] activity is lost — perceiving, transitioning, refreshing")
            state = self._regain_bearings(state)
        else:
            state = self._perceive()
        self._fresh = state
        self.goal = self._next_goal(result, state)

        intent = self._to_intent(self.goal, state) if self.goal is not None else None
        where = getattr(state, "state", None)
        if intent is not None:
            intent = self._afforded_here(intent, where, state)
        if intent is not None:
            # DO NOT RE-DISPATCH A TRANSITION THAT HAS NOT LANDED YET. Entering a building
            # takes a walk across the port, so the state stays `port_overworld` for several
            # ticks after the tap — and asking again produces the same intent every time.
            #
            # Live 2026-08-26 at Lisboa that fired ENTER_BUILDING on every tick. Re-tapping
            # mid-walk is the hazard `navigate_to_building`'s TAP_RETRY_COOLDOWN exists for:
            # the queued tap can land INSIDE the building once the scene loads, on whatever
            # happens to be under it.
            #
            # This is not a wait. The tick still perceives, still asks the task runner, and
            # still runs an activity if one is resolved — it simply does not repeat an action
            # whose effect has not been observed. When the state changes, the same intent may
            # be dispatched again freely.
            # "HAS IT LANDED?" IS A QUESTION ABOUT THE SCREEN, NOT THE STATE LABEL.
            #
            # A state label is coarse on purpose, so an action can have a real effect without
            # moving it. Live 2026-08-30 at Svear: the bot pressed Back to leave the village,
            # a `Ducat` tooltip was open (opened by a false daily-news dismissal moments
            # earlier), and Back closed the TOOLTIP. The screen changed; `village` did not.
            # The guard read that as "my press did nothing", refused the second press that
            # would actually have left, and the run stopped one leg from home.
            #
            # The signature is the SET OF ELEMENT LABELS, deliberately not pixels: walking
            # across a port animates every frame, and a pixel test would call that "changed"
            # and re-tap the building entry — the hazard this guard exists for. Labels are
            # stable while the same screen animates and move when something opens or closes.
            landed = self._in_flight == (str(intent), where, self._screen_signature(state))
            if landed:
                self._in_flight_looks += 1
                # A WALK THAT HAS HAD ITS TIME AND CHANGED NOTHING IS A TAP THAT NEVER LANDED.
                #
                # "Letting it land" is right WHILE the transition is still happening. Once its
                # declared settle has been waited out and the screen is still the same, the
                # thing being waited for did not start, and waiting again only spends the
                # stall budget: live 2026-09-02 at Faro the market tap did not register, and
                # this waited 20s three times over ninety seconds without ever tapping again,
                # then failed the mission on `trim_before_gather`.
                #
                # Re-tapping is safe HERE and nowhere earlier, which is the whole point of the
                # settle: the hazard it guards against is a retap issued MID-walk landing
                # inside the destination (on an NPC in the inn), and by now the walk would
                # long since have finished. Bounded by the stall guard above — a re-tap does
                # not change the screen either, so three unchanged ticks still end it.
                name = getattr(intent, "name", "")
                settle = _INTENT_SETTLE_S.get(name)
                exit_is_safe_here = (name != "EXIT_BUILDING"
                                     or str(where or "").split(":", 1)[0] in _RETRY_EXIT_FROM)
                if (name in _RETRY_ONCE_IF_UNCHANGED and exit_is_safe_here
                        and self._in_flight_looks == 1):
                    # ONCE, not once per tick. Repeating on every unchanged look would be the
                    # 2026-08-26 Lisboa failure again at 20-second spacing. One re-tap takes a
                    # ~5% drop rate to ~0.25%; past that the screen is not refusing a tap, and
                    # the stall guard ends it with a diagnosis rather than more taps.
                    waited = f"has had its {settle:.0f}s and " if settle else ""
                    logger.info(f"[dispatch] {intent} {waited}the screen is unchanged "
                                "— the input did not land; dispatching once more")
                    self._dispatch(intent)
                    self._fresh = None
                else:
                    logger.info(f"[dispatch] {intent} already dispatched from {where!r} and "
                                "the screen has not changed — letting it land")
            else:
                logger.info(f"[dispatch] dispatching {intent}")
                if getattr(intent, "name", None) in self._LEAVING_INTENTS:
                    # WE ASKED TO MOVE, so where we were standing is history — see
                    # `_carry_on_standing`. Dropping it here is what keeps that rule from
                    # outliving its evidence.
                    self._standing_in, self._standing_looks = None, 0
                self._in_flight = (str(intent), where, self._screen_signature(state))
                self._in_flight_looks = 0
                self._dispatch(intent)      # the ONE place a transition happens
                # AN INTENT CHANGES THE SCREEN, SO THE LOOK THAT PRECEDED IT IS SPENT.
                # `_fresh` is captured above, BEFORE this dispatch, and handed to the next
                # tick — which would then decide on a world this intent has already moved.
                #
                # Live 2026-09-04 at Svear, and it ended the mission: EXIT_BUILDING pressed
                # Back at 11:42:22, dismissing a `Cannot Exchange` notice. The next tick
                # reused the pre-Back look, still saw that notice, and tapped its OK at
                # 11:42:25 — three seconds before the next perceive at 11:42:30. By then the
                # notice was gone and the tap landed on the village screen beneath, on the
                # Weekly Reward, opening a Gear Info modal nothing had asked for.
                #
                # The staleness is old; it became harmful when the dialog check began TAPPING
                # on `_fresh` (cce9d1a). Either way the rule is the architecture's own: never
                # act on a reading the world has already contradicted.
                self._fresh = None
        else:
            self._in_flight = None

        self._set_wake_timer(result, intent)
        return {"state": getattr(state, "state", None), "goal": self.goal,
                "result": result, "intent": intent}
