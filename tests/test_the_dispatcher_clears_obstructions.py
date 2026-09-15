"""The dispatcher's obstruction clearing must actually be switched on.

`Dispatcher.step` has cleared obstructions since it was written:

    if self._unblock is not None and self._unblock(state):
        logger.info("[dispatch] cleared an obstruction — re-perceiving")
        state = self._perceive()

but production built the Dispatcher without an `unblock`, so the branch never ran (found
2026-08-28). That is why `buy_to_goal` grew FIVE `clear_blockers` calls of its own — the
shared one existed, was correct, and was switched off. An interruption is ORTHOGONAL to the
state and can sit over a village, a market or the sea; clearing it belongs in the one place
that sees every tick.
"""
import inspect
import types

from brain.dispatcher import ActivityResult, Dispatcher, FINISHED


def test_production_wires_an_unblock():
    """Assert the WIRING, not the source text.

    This grepped `run_goal`'s source for "unblock=", which broke the moment the Dispatcher
    construction was lifted into a shared helper so `run_task` could use the same wiring —
    the unblock was still passed, in a function the grep no longer read. Catching a
    real Dispatcher and looking at what it was given survives that, and is what the test
    means: production must hand it a callable, or the branch is dead again.
    """
    from unittest import mock

    from brain import run_goal

    seen = {}

    class _Spy(Dispatcher):
        def __init__(self, **kw):
            seen.update(kw)
            raise _Stop()

    class _Stop(Exception):
        pass

    with mock.patch.object(run_goal, "Dispatcher", _Spy):
        for entry, arg in ((run_goal.run_goal, object()), (run_goal.run_task, mock.Mock())):
            seen.clear()
            try:
                entry(arg)
            except _Stop:
                pass
            assert callable(seen.get("unblock")), (
                f"{entry.__name__} built a Dispatcher with no unblock — the branch is dead")


def _perceives_in_one_tick(*, blocked: bool) -> int:
    """How many times a single tick looks at the screen."""
    looks = {"n": 0}

    class _Act:
        name = "market"

        def work(self, goal, state):
            return ActivityResult(FINISHED, {}, detail="done")

    def perceive():
        looks["n"] += 1
        return types.SimpleNamespace(state="market", port="London")

    d = Dispatcher(perceive=perceive, activities={"market": _Act()},
                   next_goal=lambda r, s: "goal" if r is None else None,
                   to_intent=lambda g, s: None, dispatch=lambda i: None,
                   unblock=lambda _s: blocked)
    d.step()
    return looks["n"]


def test_a_cleared_obstruction_costs_exactly_one_extra_look():
    """Differential, because a tick ALWAYS perceives twice on its own — once to decide, and
    once in `_advance`, since an activity finishing means the world may be somewhere else.
    Clearing something adds a third."""
    assert _perceives_in_one_tick(blocked=True) == _perceives_in_one_tick(blocked=False) + 1


def test_nothing_covering_the_screen_costs_nothing():
    """The check must be cheap when it finds nothing, or it taxes every tick."""
    assert _perceives_in_one_tick(blocked=False) == 2


# ── AN INTENT DISPATCHED UNDER A MODAL WAS SWALLOWED ─────────────────────────────────
#
# Live 2026-09-14, and it ended the Hutu mission at Oslo. The globe was tapped at 16:20:41
# with a "Moon Rabbit's Part Gift Package" promo covering the middle of the screen. The game
# blocks input outside a card it has raised, so the tap did nothing. `clear_blockers`
# dismissed the promo correctly at 16:21:07 — that half worked — and the three ticks that
# followed stood on a clean port with the globe plainly visible:
#
#     16:21:21  OPEN_WORLD_MAP ... already dispatched and the screen has not changed
#     16:21:29  OPEN_WORLD_MAP ... already dispatched and the screen has not changed
#     16:21:36  NOTHING CHANGED for 3 ticks (state='port_overworld') — stopping
#
# The tap it was waiting for had been eaten twenty-six seconds earlier. The guard cannot tell
# "still landing" from "never landed", because the screen it watches genuinely did not move —
# and on a port the family classifier answers with certainty, so `_screen_signature` collapses
# to its verdict, identical with the card up and with it gone. The promo's whole life was
# invisible to the key.
#
# `_RETRY_ONCE_IF_UNCHANGED` could not save it either, and rightly excludes OPEN_WORLD_MAP:
# the globe rail TOGGLES, and re-tapping it on every unchanged tick opened and closed the map
# seventeen times in four minutes on 2026-09-01.


def _tick_with_an_intent_in_flight(*, blocked: bool):
    """One tick with a key already recorded for the intent the tick will want again.

    The key must match what the dispatcher itself would compute, or the guard sees a changed
    screen and re-dispatches for the ordinary reason — which is not what these tests are
    about. So the signature is taken from `_screen_signature` rather than written down.
    """
    from brain.intents import Intent

    dispatched = []
    intent = Intent("OPEN_WORLD_MAP", {"purpose": "choose port 'Gijón'", "where": "Gijón"})
    state = types.SimpleNamespace(state="port_overworld", port="Oslo")

    # NO ACTIVITY SERVES THE PORT OVERWORLD HERE, which is the live shape: the run logged
    # "no activity for state 'port_overworld' — asking for a goal", and the tick's whole
    # output was the transition intent. An activity that handled the goal would finish it and
    # the guard would never be reached.
    d = Dispatcher(perceive=lambda: state,
                   activities={},
                   next_goal=lambda r, s: "goal",
                   to_intent=lambda g, s: intent,
                   dispatch=lambda i: dispatched.append(i),
                   unblock=lambda _s: blocked)
    d._in_flight = (str(intent), "port_overworld", d._screen_signature(state))
    d._in_flight_looks = 2
    d.step()
    return d, dispatched


def test_CLEARING_AN_OBSTRUCTION_DROPS_THE_IN_FLIGHT_KEY():
    """The key is evidence about a world that no longer exists — a stored conclusion
    outliving what it was drawn from, which this codebase does not keep."""
    d, dispatched = _tick_with_an_intent_in_flight(blocked=True)
    assert dispatched, "with the key gone the tick must be free to act again"
    assert dispatched[0].name == "OPEN_WORLD_MAP"


def test_the_key_survives_a_tick_with_nothing_in_the_way():
    """Only an OBSERVED clear invalidates it. An ordinary unchanged tick must still wait for
    the tap to land, or this becomes the toggle loop of 2026-09-01 under another name — the
    globe rail toggles, so a second tap CLOSES the map a first one opened."""
    d, dispatched = _tick_with_an_intent_in_flight(blocked=False)
    assert dispatched == [], "an unchanged screen is not evidence the tap was swallowed"
    assert d._in_flight is not None


def test_IT_IS_THE_CLEAR_THAT_FREES_THE_TICK_NOT_THE_TICK_ITSELF():
    """Stated as a differential, because the two tests above are only meaningful together:
    the SAME tick, the same screen, the same key — and the only thing that differs is whether
    something was observed covering the world and taken away.

    Compared on what was DISPATCHED, not on the key. After the drop the tick re-dispatches
    and records the key again, with the same value it had, so the keys are equal at the end
    of both ticks and say nothing."""
    _cleared, after_clear = _tick_with_an_intent_in_flight(blocked=True)
    _waited, after_wait = _tick_with_an_intent_in_flight(blocked=False)
    assert after_clear and not after_wait


# ── A TAP THAT CHANGED NOTHING HAS TWO EXPLANATIONS, AND ANDROID KNOWS WHICH ─────────
#
# The pixels cannot separate them: the input was DROPPED, or something else held focus and
# swallowed it. Both happened on 2026-09-14. At Oslo a promo card sat over the port and the
# globe tap went nowhere, and the cause was on screen. At Hutu the Ok on the Barter
# Calculations card vanished TWICE with nothing visible over it — the scene animating behind
# it, the button shimmering on its own — and the mission died. Tapped by hand afterwards the
# game answered at once, so that one was the ~4.8% drop rate `TAP_DRIFT_MAX` documents,
# landing twice running.
#
# The two call for opposite fixes, so the run must be able to say which it met. Android
# authors the window stack and can simply be asked.


def _focus_note(window):
    from unittest import mock

    import brain.dispatcher as D

    with mock.patch("actions.adb_actions.focused_window", return_value=window):
        return D._who_has_focus()


def test_the_game_holding_focus_means_the_tap_was_DROPPED():
    note = _focus_note("com.linegames.uwogl/com.epicgames.ue4.GameActivity")
    assert "dropped" in note


def test_ANOTHER_WINDOW_HOLDING_FOCUS_IS_THE_LOUD_CASE():
    """A stolen tap is a different defect from a dropped one, and the line must not read
    like the ordinary case."""
    note = _focus_note("com.android.systemui/…ShadeWindow")
    assert "not the game" in note
    assert "dropped" not in note


def test_an_unreadable_focus_says_NOTHING_rather_than_guessing():
    """The suffix is appended to a real log line, so a failed read must add no words at all
    — a diagnostic that invents an answer is worse than one that stays quiet."""
    assert _focus_note(None) == ""


def test_asking_costs_nothing_on_the_happy_path():
    """Read off the source: it must be reached only from the branches that have ALREADY
    observed an unchanged screen, never from the ordinary dispatch."""
    import inspect

    import brain.dispatcher as D

    src = inspect.getsource(D.Dispatcher._advance)
    for line in src.splitlines():
        if "_who_has_focus()" in line:
            continue
        assert "_who_has_focus" not in line
    assert src.count("_who_has_focus()") == 2, "the two unchanged-screen branches, and no more"
