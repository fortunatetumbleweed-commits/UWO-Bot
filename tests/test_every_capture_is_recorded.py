"""Recording hangs off the CAPTURE, not off what we later decide the capture meant.

LIVE 2026-08-30 at Hutu Village. The run ended on "no tradable goods tiles found on the Barter
panel". `_select_trade_good` captured a screen, parsed it, found nothing, and returned WITHOUT
tapping — and the tracer hangs a frame on each tap/back primitive, so nothing was written. The
trace stopped at the previous tap and the one screen that explained the failure was the one
screen never saved.

Capture precedes all interpretation, so it is where recording belongs. `capture/` must not
import `actions/` (layering), so the tracer registers a sink instead.

This is a floodlight over the captures the code makes TODAY — 215 scattered call sites. It is
not the fix. Capture belongs in perceive, once per tick, handed down to whoever reads it.
"""
import unittest
from unittest.mock import patch


class _Img:
    def __init__(self): self.saved = []
    def save(self, path): self.saved.append(str(path))


# Bound at import, BEFORE conftest's autouse fixture swaps the module attribute for a blank-
# image stub — so this is the real implementation. Its own `_capture_screen_raw` lookup still
# resolves to conftest's stub, so nothing here touches a device.
from capture.adb_capture import capture_screen as _real_capture_screen


class TheSinkSeesEveryFrame(unittest.TestCase):
    def tearDown(self):
        from capture.adb_capture import set_capture_sink
        set_capture_sink(None)

    def test_a_capture_reaches_the_sink(self):
        from capture.adb_capture import set_capture_sink
        seen = []
        set_capture_sink(seen.append)
        got = _real_capture_screen()
        self.assertEqual(len(seen), 1, "the frame is recorded before anyone reads it")
        self.assertIs(seen[0], got, "and it is the very frame handed to the caller")

    def test_a_broken_sink_never_costs_the_frame(self):
        from capture.adb_capture import set_capture_sink
        def boom(_f): raise RuntimeError("disk full")
        set_capture_sink(boom)
        self.assertIsNotNone(_real_capture_screen(),
                             "recording must never cost us the frame we came for")

    def test_no_sink_is_the_default(self):
        self.assertIsNotNone(_real_capture_screen())


class TheRecorderDoesNotRecurse(unittest.TestCase):
    def test_recording_a_tap_does_not_re_enter_via_its_own_capture(self):
        from actions import action_trace
        action_trace._in_record = True          # as it is while record_tap captures
        try:
            action_trace.record_capture(_Img())  # must be a no-op, not a second frame
        finally:
            action_trace._in_record = False

    def test_the_flag_is_released_even_when_recording_fails(self):
        from actions import action_trace
        import pathlib
        action_trace._dir = pathlib.Path("/nonexistent-dir-for-this-test")
        try:
            action_trace.record_capture(_Img())   # save() will fail on a bad dir
            self.assertFalse(action_trace._in_record,
                             "a stuck flag would silently disable recording for the whole run")
        finally:
            action_trace._dir = None


if __name__ == "__main__":
    unittest.main()
