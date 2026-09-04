"""The sea is somewhere the dispatcher meets — it used to be somewhere two loops owned.

`"no activity for state 'sea'"` appears ZERO times in the 2026-08-28 logs, not because the
sea was handled but because `drive_sail_to` and `_await_route_arrival` held the fleet for the
whole voyage. Arriving at London the screen was the idle lock with the daily news on top, and
the route loop polled `_current_port()` — a FIELD READ, not a perceive — so it received
neither the interruptor pass nor `IdleLockActivity`. It would have polled four hours past a
popup one ordinary perceive removes, while the lock displayed the port name it was waiting for.
"""
import types

import pytest

from brain.activities.sea import SeaActivity
from brain.dispatcher import BLOCKED, WORKING

STATE = types.SimpleNamespace(state="sea", port=None)


def _sea(speed=None, **hud):
    """`speed` defaults to None — the tile cannot be read — which also keeps these tests off
    the real reader, whose `locate=True` costs an OmniParser parse per call."""
    return SeaActivity(hud_fn=lambda: hud, speed_fn=lambda: speed,
                       checkback_fn=lambda d: 360.0)


def test_sailing_reports_WORKING_not_a_stall():
    """The reason this activity exists. With no activity the dispatcher reports
    UNRECOGNISED every tick, and run_goal counts those as stalls — six of them and a voyage
    is abandoned. Only an activity can say 'one step taken, same context, ask me again'."""
    res = _sea(supply_days=8, eta_days=3, destination="London").work(None, STATE)
    assert res.status == WORKING


def test_it_takes_no_action_at_all():
    """An activity with nothing to do is not a defect. The value is that the TICK
    perceived — which is where interruptor dismissal lives."""
    import inspect
    src = inspect.getsource(SeaActivity.work)
    for verb in ("tap(", "swipe(", "press_back", "sail_to_port", "navigate"):
        assert verb not in src, f"the sea activity must not {verb}"


def test_it_never_asks_whether_it_has_arrived():
    """Arrival is a context change to notice, not an event to wait for. When perceive stops
    saying `sea`, the dispatcher does not call this at all."""
    import inspect
    src = inspect.getsource(SeaActivity)
    assert "_current_port" not in src
    assert "arriv" not in src.lower().split('"""')[-1], "no arrival check in the code body"


def test_a_dry_fleet_is_reported_not_decided():
    """Whether to press on or turn back needs the destination and the mission — neither is
    this activity's to weigh.

    It used to answer BLOCKED here, which IS a decision: it ended the leg. The days aboard now
    ride on the observation and `SailRunner` weighs them against the leg's floor, which is the
    only place that knows what the floor is. "Reported, not decided" is what the name always
    said; the status was the last part still deciding.
    """
    res = _sea(supply_days=1, eta_days=4, destination="London").work(None, STATE)
    assert res.observed["supply_days"] == 1, "the number reaches the task runner"
    assert res.status != BLOCKED, "one day of supply is not this activity's call to make"


def test_the_pacing_travels_with_the_observation():
    """`_await_route_arrival` slept inside its loop; the cadence now rides on the result and
    the tick loop honours it. Pacing belongs to the STATE, not to a procedure.

    The ship has to be CONFIRMED under way for the state's own cadence to apply: until then
    the wait is short, because minutes-long checkbacks are right for a voyage in progress and
    useless for noticing one that never began.
    """
    act = _sea(speed=9.0, supply_days=5, eta_days=2)
    # Three looks first — a voyage is not paced off one reading (see the warm-up test below).
    first = act.work(None, STATE)
    assert first.observed["checkback_s"] == act._WARMUP_CHECKBACK_S
    act.work(None, STATE)
    res = act.work(None, STATE)
    assert res.observed["checkback_s"] == 360.0        # what the injected seam returns


def test_an_unreadable_hud_still_ticks():
    """A HUD that cannot be read is not a reason to stop sailing — it is a reason to look
    again shortly."""
    res = SeaActivity(hud_fn=lambda: {}, speed_fn=lambda: None).work(None, STATE)
    assert res.status == WORKING
    assert res.observed["checkback_s"] > 0


def test_the_dispatcher_meets_the_sea_now():
    """The registry holds a LIST per state now — a state may have several activities,
    resolved by their GOALS filter."""
    from brain.run_goal import default_activities
    reg = default_activities()
    for where in ("sea", "sea_cinematic"):
        assert any(isinstance(a, SeaActivity) for a in reg[where]), where


def test_the_dispatcher_honours_the_reported_cadence():
    """The activity STATES a wake time; the DISPATCHER waits it out (user, 2026-08-31).

    This used to read `run_goal`'s source, because that loop did the sleeping. The waiting
    moved into the dispatcher, because what to do on waking is a ROUTING decision — arrived at
    a port, still at sea, idle-locked — and the loop does not route. What must remain true is
    unchanged: the cadence is whatever the state asked for, and the sleep is jittered, because
    a fixed cadence is what the anti-cheat watches for.
    """
    import inspect

    from brain.dispatcher import Dispatcher
    setting = inspect.getsource(Dispatcher._set_wake_timer)
    waiting = inspect.getsource(Dispatcher._wait_out_the_wake_timer)

    assert "checkback_s" in setting, "the wake time is what the state asked for"
    assert "_sleep_jittered" in waiting, "never a fixed cadence — the anti-cheat watches for it"
    # The fresh look is NOT here: `step` asks for one on every pass, waited or not. Binding
    # it to the wait made a step that neither waited nor acted re-read a frame it already
    # held — which wedged a live run on 2026-08-31, blind to a popup on screen.
    from brain.dispatcher import Dispatcher as _D
    assert "expect_changed" in inspect.getsource(_D.step)


def test_a_tick_that_acted_does_not_sleep():
    """NEVER SLEEP ON A TICK THAT ACTED.

    Live 2026-08-29 the sea asked for 7.5 minutes mid-voyage, and by the end of that same tick
    the fleet had reached Tripoli, been handed 'hold Candle (~934)' and tapped into the
    market — then slept 7.5 minutes on the market's doorstep. A dispatched intent is proof the
    world moved on: there is something to look at NOW.
    """
    from brain.dispatcher import ActivityResult, Dispatcher, WORKING

    d = Dispatcher.__new__(Dispatcher)
    d._wake_at, d._wake_why = 0.0, ""
    sailing = ActivityResult(WORKING, {"checkback_s": 450.0, "eta_days": 5}, detail="at sea")

    d._set_wake_timer(sailing, intent=None)
    assert d._wake_at > 0, "still at sea, nothing dispatched — wait as asked"

    d._set_wake_timer(sailing, intent="ENTER_BUILDING(market)")
    assert d._wake_at == 0.0, "arrived and dispatched — do not sleep on the doorstep"

    d._set_wake_timer(ActivityResult(WORKING, {}, detail="no pause asked for"), intent=None)
    assert d._wake_at == 0.0


class AStateMayHaveMoreThanOneActivity:
    """`village` is served by VillageActivity for a Barter and AshoreActivity for an
    ArriveAshore — the same screen, two different jobs. Registering one activity per state
    silently overwrote whichever came first (found 2026-08-28: AshoreActivity clobbered
    VillageActivity because it registered later, and every barter would have reported
    'ashore' instead of bartering).

    This is the intent-filter resolution the design describes: SERVES filters on the STATE,
    GOALS on the ORDER, and the pair identifies the handler.
    """


def _dispatcher():
    from brain.dispatcher import Dispatcher
    from brain.run_goal import default_activities
    return Dispatcher(perceive=lambda: None, activities=default_activities(),
                      next_goal=lambda a, b: None, to_intent=lambda a, b: None,
                      dispatch=lambda i: None)


def test_the_same_state_resolves_to_different_activities_by_goal():
    from brain.activities.sea import ArriveAshore, AshoreActivity
    from brain.activities.village import Barter, VillageActivity
    d = _dispatcher()
    assert isinstance(d._pick("village", Barter("Birch Tree", "Svear")), VillageActivity)
    assert isinstance(d._pick("village", ArriveAshore()), AshoreActivity)


def test_arriving_ends_the_voyage_rather_than_looking_like_a_stall():
    """Without AshoreActivity, `port_overworld` has no activity, so every tick after arrival
    reports UNRECOGNISED — and run_goal gives up after six. The fleet would arrive and the
    goal be abandoned for want of anyone to say 'done'."""
    from brain.activities.sea import ArriveAshore
    from brain.dispatcher import FINISHED
    d = _dispatcher()
    act = d._pick("port_overworld", ArriveAshore())
    res = act.work(ArriveAshore("route 'san to london'"),
                   types.SimpleNamespace(state="port_overworld", port="London"))
    assert res.status == FINISHED
    assert res.observed["port"] == "London"


def test_the_port_still_behaves_as_before_for_other_goals():
    """AshoreActivity declares GOALS=(ArriveAshore,), so goal-aware dispatch refuses to hand
    it anything else and the port keeps falling through to 'ask for a goal' — which is right,
    because a port tick HAS an intent to dispatch."""
    from brain.dispatcher import _serves_goal
    from brain.activities.sea import AshoreActivity
    assert not _serves_goal(AshoreActivity(), object())


def test_the_eta_paces_the_look_not_the_supply():
    """WAKE BY THE END OF THE LEG. One game day is 90 real seconds, so a one-day leg is
    looked at again in 1.5 minutes (user, 2026-08-29). Supply may only SHORTEN that — a fleet
    running dry needs attention sooner — never extend it past the leg.

    Live 2026-08-29: supply read 413 days on a fleet carrying 13.5, and pacing on supply
    alone set the next look 618 minutes out. The fleet reached Amsterdam, the phone
    idle-locked, and the bot was still asleep. The arithmetic was right; the question was.
    """
    def _paced(supply, eta):
        """A voyage past its warm-up, every reading agreeing."""
        act = SeaActivity()
        act._moving = True
        act._etas += [float(eta)] * 3
        act._supplies += [float(supply)] * 3
        return act._checkback_seconds(supply, eta)

    # the ETA governs, whatever supply claims
    assert _paced(413, 1) == pytest.approx(90.0)
    assert _paced(13.5, 4) == pytest.approx(360.0)

    # supply shortens it when the fleet is nearly dry
    assert _paced(2, 8) < _paced(13.5, 8)

    # and a misread ETA cannot buy hours of silence
    assert _paced(13.5, 400) <= 1200.0


def test_a_voyage_is_looked_at_a_few_times_before_it_is_left_alone():
    """LOOK THREE TIMES BEFORE LOOKING AWAY (user, 2026-08-29).

    The first reading of a voyage is one sample of a reader known to slip. Pacing on it
    alone let a single '415 days of supply' schedule the next look 618 minutes out; the
    fleet reached Amsterdam, the phone idle-locked, and the bot slept through the arrival.

    Three short looks cost about a minute, and they give the readings something to disagree
    with — the LOWEST reading then wins, in both directions. Supply falls and the ETA falls,
    so the smallest is nearest to now, and a spuriously HIGH one can only ever lengthen a
    sleep, which is the failure being guarded. Taking the minimum makes a bad reading
    harmless without having to decide which reading was bad.
    """
    act = SeaActivity()
    act._moving = True

    # a voyage whose supply readings are 415, 15, 115 — the true value is 15, ETA 5 days
    waits = []
    for days, eta in [(415, 5), (15, 5), (115, 5)]:
        act._etas.append(float(eta))
        act._supplies.append(float(days))
        waits.append(act._checkback_seconds(days, eta))

    assert waits[0] == act._WARMUP_CHECKBACK_S, "slept on the first reading"
    assert waits[1] == act._WARMUP_CHECKBACK_S, "slept on the second"
    # a ship not yet seen to move is looked at sooner still — the two are not the same rule
    assert act._UNCONFIRMED_CHECKBACK_S < act._WARMUP_CHECKBACK_S
    # by the third the voyage is paced — on the ETA, not on the 115 that was just read
    assert waits[2] == pytest.approx(5 * 90.0)


def test_one_wild_supply_reading_cannot_stretch_the_sleep():
    """The minimum across the voyage is what paces it, so a high outlier is inert."""
    honest, lied = SeaActivity(), SeaActivity()
    for act, readings in ((honest, [15, 15, 15]), (lied, [15, 415, 15])):
        act._moving = True
        for days in readings:
            act._etas.append(40.0)          # a long ETA, so supply is what decides
            act._supplies.append(float(days))
    assert (honest._checkback_seconds(15, 40)
            == lied._checkback_seconds(415, 40)), "the outlier changed the pacing"
