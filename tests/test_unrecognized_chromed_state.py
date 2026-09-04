"""An unsupported chromed screen is a STATE, not a recovery.

It differs from `market` only in its repertoire — market has buy/sell/exit, this has back
and home — and it FINISHES the same way, leaving the dispatcher to re-perceive and route.

The alternative is what `recover_to_port_overworld` does: own a 20-attempt loop and treat
`port_overworld` as the only success. Live 2026-08-27 that killed a run. The daily news
knocked the bot to the main menu just after it committed a departure; recovery closed the
menu correctly, perception read `sea` at confidence 1.00 — the fleet was sailing to Tripoli
exactly as ordered — and recovery declared failure because sea is not port_overworld.
"""
import types

import pytest

from brain.activities.unrecognized_chromed import STATE, UnrecognizedChromedActivity
from brain.dispatcher import FINISHED


def _chrome(home=True, pos=(2219, 44)):
    return types.SimpleNamespace(has_home=home, scores={"home": 0.97 if home else 0.1},
                                 positions={"home": pos} if home and pos else {})


def _activity(**kw):
    calls = {"taps": [], "backs": []}
    act = UnrecognizedChromedActivity(
        capture=lambda: object(), tap=lambda x, y: calls["taps"].append((x, y)),
        back=lambda: calls["backs"].append(True), settle=lambda _k: None,
        chrome=lambda _f: _chrome(**kw))
    return act, calls


def test_it_is_registered_as_a_state_like_any_other():
    """The registry holds a LIST per state — a state may have several activities, resolved
    by their GOALS filter (village is served by VillageActivity for a Barter and by
    AshoreActivity for an ArriveAshore)."""
    from brain.run_goal import default_activities
    assert any(isinstance(a, UnrecognizedChromedActivity)
               for a in default_activities()[STATE])


def test_home_is_the_preferred_exit():
    act, calls = _activity()
    res = act.work(goal=None, state=types.SimpleNamespace(frame=None))
    assert res.status == FINISHED and calls["taps"] == [(2219, 44)] and not calls["backs"]


def test_it_taps_where_the_icon_MATCHED_not_a_constant():
    """On the live Placement Setting frame Home matched at (2219,44) while the calibrated
    MARKET_COORDS['home'] says (2350,40) — 131px away, and would have hit nothing."""
    act, calls = _activity(pos=(2219, 44))
    act.work(goal=None, state=types.SimpleNamespace(frame=None))
    from config.settings import MARKET_COORDS
    assert calls["taps"][0] != MARKET_COORDS["home"]


def test_back_is_the_fallback():
    act, calls = _activity(home=False)
    res = act.work(goal=None, state=types.SimpleNamespace(frame=None))
    assert res.status == FINISHED and calls["backs"] and not calls["taps"]


def test_a_detected_home_without_a_position_is_not_tapped():
    act, calls = _activity(pos=None)
    act.work(goal=None, state=types.SimpleNamespace(frame=None))
    assert calls["taps"] == [] and calls["backs"]


def test_it_never_reports_where_it_landed():
    """Reporting a destination would hand the task runner a belief dressed as an
    observation — the dispatcher re-perceives after every activity."""
    act, _ = _activity()
    res = act.work(goal=None, state=types.SimpleNamespace(frame=None))
    assert "port_overworld" not in str(res.observed) + res.detail
    assert set(res.observed) <= {"exit", "at"}


def test_it_acts_once_per_call_so_it_cannot_outlive_a_world_change():
    """The property that makes the looping recovery fatal: one call, one action."""
    act, calls = _activity()
    act.work(goal=None, state=types.SimpleNamespace(frame=None))
    assert len(calls["taps"]) + len(calls["backs"]) == 1


def test_perceive_names_the_state_rather_than_calling_it_a_building():
    import inspect

    import brain.perceive as P
    src = inspect.getsource(P._classify_nav_state)
    assert '"location": "unrecognized_chromed_screen"' in src, \
        "the chromed gate must not report 'building' — no activity serves it, so the " \
        "dispatcher goes looking for a building to enter"
