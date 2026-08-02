# tests/test_orientation_guard.py
# Orientation guard — parsing + ensure_canonical_orientation logic, adb mocked.

import pytest

from actions import orientation as orient
from config.settings import CANONICAL_ROTATION

_DUMPSYS = (
    'foo mBaseDisplayInfo=... real 2400 x 1080 ... \n'
    '    mDisplayRotation=ROTATION_{rot} mRotation=ROTATION_{rot}\n'
)


def _fake_adb(rotation, autorotate="0"):
    """Return an _adb_out stub answering by command, recording writes."""
    writes = []

    def _stub(args):
        if args[:2] == ["shell", "dumpsys"]:
            return _DUMPSYS.format(rot=rotation)
        if args[:4] == ["shell", "settings", "get", "system"]:
            return autorotate + "\n"
        if args[:3] == ["shell", "settings", "put"]:
            writes.append(args)
            return ""
        return ""

    _stub.writes = writes
    return _stub


def test_parse_rotation():
    assert orient._parse_rotation("x mDisplayRotation=ROTATION_270 y") == 270
    assert orient._parse_rotation("mDisplayRotation=ROTATION_90") == 90
    assert orient._parse_rotation("no rotation field here") is None


def test_ensure_ok_when_canonical(monkeypatch):
    monkeypatch.setattr(orient, "_adb_out",
                        _fake_adb(CANONICAL_ROTATION, autorotate="0"))
    assert orient.ensure_canonical_orientation() is True


def test_ensure_locks_autorotate_when_on(monkeypatch):
    stub = _fake_adb(CANONICAL_ROTATION, autorotate="1")
    monkeypatch.setattr(orient, "_adb_out", stub)
    assert orient.ensure_canonical_orientation() is True
    # auto-rotate was ON → guard must have written accelerometer_rotation 0
    assert any(w[:3] == ["shell", "settings", "put"]
               and w[-2:] == ["accelerometer_rotation", "0"]
               for w in stub.writes)


def test_ensure_returns_false_on_mismatch(monkeypatch):
    wrong = 90 if CANONICAL_ROTATION != 90 else 270
    monkeypatch.setattr(orient, "_adb_out", _fake_adb(wrong, autorotate="0"))
    assert orient.ensure_canonical_orientation(raise_on_mismatch=False) is False


def test_ensure_raises_on_mismatch(monkeypatch):
    wrong = 90 if CANONICAL_ROTATION != 90 else 270
    monkeypatch.setattr(orient, "_adb_out", _fake_adb(wrong, autorotate="0"))
    with pytest.raises(orient.OrientationError):
        orient.ensure_canonical_orientation()
