# tests/test_a_port_still_drawing_is_not_decided_on.py
#
# A port arrives in two stages: the 3-D scene draws, then the UI lands a beat later. In
# between, the frame is unmistakably a port to a classifier that reads pixels and carries
# none of the things a port is read FOR.
#
# Live 2026-09-02, four seconds after leaving the sea for Faro:
#
#     15:56:25  → port_overworld (family-classifier conf=1.00, port=None) — short-circuit
#     15:56:27  the voyage ended at 'Lisboa', not 'Faro' — the leg is not done
#     FAILED at step mission: gather:Faro
#
# The fleet was standing in Faro. The name banner had not drawn, so `read_port_name`
# correctly returned None, and the arrival check compared the destination against a name
# that had come from somewhere other than that frame.
#
# CLAUDE.md already states the invariant — "a port_overworld ALWAYS has a port name; failing
# to read one there is an anomaly to flag + retry, NOT a silent None" — and nothing enforced
# it. This is that enforcement, keyed on the right-edge cluster rather than the name, because
# the cluster is structural where the name is one OCR field.
#
# THE CASE THAT MUST NOT REGRESS: tapping the lighthouse icon opens the port-info overlay,
# which DIMS the chrome without removing it (user, 2026-09-02). A dimmed port is drawn.

import pathlib

import pytest

pytestmark = pytest.mark.functional

_TRACE = pathlib.Path("data/sessions/trace_barter_cmd_2026-09-02T15-51-39")
_MID_RENDER = _TRACE / "frame_0033.png"      # Faro, 4s after the sea — UI not landed
_SETTLED = _TRACE / "frame_0031.png"         # Lisboa, fully drawn


def _img(p):
    if not p.exists():
        pytest.skip(f"{p} not in the repo (real-frame fixture)")
    from PIL import Image
    return Image.open(p)


def test_the_mid_render_frame_is_not_drawn():
    """The frame the mission was failed on."""
    from vision.chrome_via_omniparser import port_overworld_is_drawn
    assert port_overworld_is_drawn(_img(_MID_RENDER)) is False


def test_a_settled_port_is_drawn():
    from vision.chrome_via_omniparser import port_overworld_is_drawn
    assert port_overworld_is_drawn(_img(_SETTLED)) is True


def test_the_name_is_unreadable_exactly_where_the_cluster_is_missing():
    """Ties the two halves together: the missing cluster and the missing name are the same
    moment, so the cluster is a sound proxy for 'the name is not there yet'."""
    from vision.ocr import read_port_name
    from vision.chrome_via_omniparser import port_overworld_is_drawn
    mid = _img(_MID_RENDER)
    assert read_port_name(mid) is None
    assert port_overworld_is_drawn(mid) is False


class TheDispatcherWaitsRatherThanDeciding:
    """Behavioural half — kept out of the real-frame class so it needs no fixtures."""


def _dispatcher(drawn: bool, where: str = "port_overworld"):
    import types
    from brain.dispatcher import Dispatcher
    d = Dispatcher(perceive=lambda: types.SimpleNamespace(state=where, port=None),
                   activities={}, next_goal=lambda *a, **k: None,
                   to_intent=lambda *a, **k: None, dispatch=lambda *a, **k: None)
    d._port_has_finished_drawing = lambda: drawn
    return d


def test_an_undrawn_port_makes_the_dispatcher_wait_not_decide():
    from brain.dispatcher import _PORT_DRAW_SETTLE_S
    d = _dispatcher(drawn=False)
    out = d.step()
    assert "drawing" in str(out.get("did", ""))
    assert d._wake_at > 0, "it must sleep on it, not spin"
    assert d._undrawn_looks == 1


def test_it_gives_up_waiting_rather_than_stranding_the_mission():
    """A cluster that never appears is a READING problem, and looping on it would strand a
    mission that could still act. Three looks is a minute — long past any render."""
    from brain.dispatcher import _MAX_UNDRAWN_LOOKS
    d = _dispatcher(drawn=False)
    for _ in range(_MAX_UNDRAWN_LOOKS):
        d.step()
    assert d._undrawn_looks == _MAX_UNDRAWN_LOOKS
    d.step()                       # the next look must NOT wait again
    assert d._undrawn_looks == 0


def test_a_drawn_port_is_not_waited_on():
    d = _dispatcher(drawn=True)
    d.step()
    assert d._undrawn_looks == 0


def test_only_ports_are_gated():
    """The sea has no right-edge building cluster and must not be waited on for lacking one."""
    d = _dispatcher(drawn=False, where="sea")
    d.step()
    assert d._undrawn_looks == 0


def test_a_check_that_cannot_see_says_drawn():
    """Unknown counts as drawn: a check that cannot look must never be the thing that stops
    the mission. Every downstream reader already copes with a missing name."""
    import types
    from brain.dispatcher import Dispatcher
    d = Dispatcher(perceive=lambda: types.SimpleNamespace(state="port_overworld", port=None),
                   activities={}, next_goal=lambda *a, **k: None,
                   to_intent=lambda *a, **k: None, dispatch=lambda *a, **k: None)
    assert d._port_has_finished_drawing() is True     # no live screen in the unit suite
