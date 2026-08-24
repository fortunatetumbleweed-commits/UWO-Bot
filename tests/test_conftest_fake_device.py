"""The offline device stand-ins in conftest.py must stay faithful to the real parsers.

A fake that drifts from the code it feeds is worse than no fake: `_parse_rotation`
looks for `mDisplayRotation=ROTATION_<n>`, and an earlier version of the conftest
fake emitted `mCurrentRotation=` instead — which parses to None, i.e. "rotation
unreadable". Every orientation-gated test would then exercise the failure path while
appearing to pass for the wrong reason.
"""
from actions.orientation import CANONICAL_ROTATION, get_display_rotation, is_autorotate_on
from capture.adb_capture import capture_screen


def test_fake_dumpsys_parses_to_canonical_rotation():
    assert get_display_rotation() == CANONICAL_ROTATION


def test_fake_reports_autorotate_locked():
    assert is_autorotate_on() is False


def test_fake_capture_returns_a_landscape_frame():
    frame = capture_screen()
    assert frame.size == (2400, 1080)
