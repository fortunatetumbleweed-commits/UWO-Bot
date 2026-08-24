"""Every input primitive must appear in the action trace.

A trace that omits some primitives reads as if the bot skipped steps — and the missing
steps are exactly the hard-to-explain moments. On 2026-08-22 a run's trace showed goods
already staged in the market cart with no action that put them there, and four of the nine
input primitives turned out to record nothing at all:

    recorded:  tap, tap_fast, swipe, swipe_fast, press_back
    invisible: long_press, wake, pinch_zoom, input_text

User: "I am not sure why the steps before it was not recorded."

This test enumerates the input surface, so a primitive added later cannot quietly become
invisible too.
"""
from unittest import mock

import pytest

import actions.action_trace as at


def _pristine_adb_actions():
    """A fresh copy of actions/adb_actions.py, unaffected by conftest's offline stubs.

    tests/conftest.py replaces every input primitive with a recording stub so no test can
    reach the phone. That stub is what a normal import would give us here, and it does not
    call action_trace — so the test would pass or fail on the stub instead of the real
    primitive. Loading the module afresh exercises the actual code.
    """
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "actions" / "adb_actions.py"
    spec = importlib.util.spec_from_file_location("adb_actions_pristine", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# name -> args to call it with
INPUT_PRIMITIVES = {
    "tap": (100, 200),
    "tap_fast": (100, 200),
    "long_press": (100, 200),
    "swipe": (10, 20, 30, 40),
    "swipe_fast": (10, 20, 30, 40),
    "press_back": (),
    "wake": (),
    "pinch_zoom": (1200, 540),
    "input_text": ("hello",),
}


@pytest.mark.parametrize("name", sorted(INPUT_PRIMITIVES))
def test_primitive_records_to_the_trace(name):
    recorded = []
    aa = _pristine_adb_actions()
    # Patch the MODULE OBJECT: the primitives do `from actions import action_trace`
    # inside the call, so a string-path patch is not necessarily bound at that moment.
    # pinch_zoom does not go through _adb — it writes multitouch events directly — so the
    # subprocess layer is stubbed as well.
    with mock.patch.object(aa, "_adb", return_value=None), \
         mock.patch.object(aa.subprocess, "run",
                           return_value=mock.MagicMock(returncode=0, stderr=b"")), \
         mock.patch.object(at, "active", return_value=True), \
         mock.patch.object(at, "record_tap",
                           side_effect=lambda *a, **k: recorded.append(a)), \
         mock.patch("time.sleep"):
        getattr(aa, name)(*INPUT_PRIMITIVES[name])
    assert recorded, f"{name}() sent input without recording it to the trace"


def test_the_input_surface_has_not_grown_unnoticed():
    """If a new input primitive appears, it must be added here (and made to record)."""
    import inspect
    aa = _pristine_adb_actions()
    sends_input = set()
    for fn_name, fn in vars(aa).items():
        if fn_name.startswith("_") or not inspect.isfunction(fn):
            continue
        try:
            src = inspect.getsource(fn)
        except OSError:
            continue
        if "_adb(" in src and fn_name not in ("_adb",):
            sends_input.add(fn_name)
    unlisted = sends_input - set(INPUT_PRIMITIVES) - {"_human_delay"}
    assert not unlisted, (
        f"these functions send input but are not covered here: {sorted(unlisted)} — "
        "add them to INPUT_PRIMITIVES and make sure they call action_trace.record_tap")
