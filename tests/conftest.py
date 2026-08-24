"""Unit tests must never touch the phone.

A test that reaches ADB taps the LIVE GAME. That is not hypothetical: on 2026-08-20 the
`pan_to_village` wrapper tests reached the real `_try_port_search` and tapped the device
from a unit test, and on 2026-08-21 a test run had to be killed mid-suite because the bot
appeared to be hunting a port with no mission active. Beyond breaking the game state, a
test that pans or zooms the world map leaves the camera and the persisted px/game SCALE
changed for the next live run — a plausible contributor to the "could not find the
destination" failures, where the scale had shifted from 0.76 to 2.01 between runs with no
mission having done it.

Rather than trusting every test to mock the right seam, this blocks the bottom of the
stack: any `adb` subprocess raises. Mocked calls are unaffected, since they never get
here. A test that genuinely needs the device must opt in explicitly:

    @pytest.mark.device
    def test_something_live(): ...

and those are skipped by default; run them with `--device`.
"""
import subprocess
import sys

import pytest

_real_run = subprocess.run
_real_check_output = subprocess.check_output
_real_popen = subprocess.Popen


def _is_adb(cmd) -> bool:
    if isinstance(cmd, (list, tuple)):
        head = str(cmd[0]) if cmd else ""
        return head.endswith("adb") or head == "adb"
    return isinstance(cmd, str) and cmd.strip().startswith("adb")


def _blocked(cmd):
    raise AssertionError(
        "This test tried to run ADB against the real device:\n"
        f"    {cmd}\n"
        "Unit tests must not touch the phone — it taps the live game and can leave the "
        "world-map camera and persisted scale changed for the next run. Mock the seam "
        "you are exercising (capture_screen, actions.adb_actions.tap, _try_port_search, "
        "…), or mark the test @pytest.mark.device if it genuinely needs hardware."
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "device: needs the real phone; skipped unless --device")


def pytest_addoption(parser):
    parser.addoption("--device", action="store_true", default=False,
                     help="run tests marked @pytest.mark.device against the real phone")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--device"):
        return
    skip = pytest.mark.skip(reason="needs the real phone; pass --device to run")
    for item in items:
        if "device" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _no_adb_from_tests(request, monkeypatch):
    """Fail any test that shells out to adb. Opted out by @pytest.mark.device."""
    if "device" in request.keywords:
        yield
        return

    def guard_run(cmd, *a, **kw):
        if _is_adb(cmd):
            _blocked(cmd)
        return _real_run(cmd, *a, **kw)

    def guard_check_output(cmd, *a, **kw):
        if _is_adb(cmd):
            _blocked(cmd)
        return _real_check_output(cmd, *a, **kw)

    def guard_popen(cmd, *a, **kw):
        if _is_adb(cmd):
            _blocked(cmd)
        return _real_popen(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", guard_run)
    monkeypatch.setattr(subprocess, "check_output", guard_check_output)
    monkeypatch.setattr(subprocess, "Popen", guard_popen)
    yield


# ── Offline device stand-ins ─────────────────────────────────────────────────
#
# The hard block above turns device access into a loud failure, but most tests
# reaching ADB only wanted a frame or the screen rotation — they were quietly
# screencapping the live phone and asserting against whatever happened to be on
# it, which makes them slow, non-hermetic, and dependent on a phone being
# plugged in.  These fixtures hand them offline stand-ins instead.
#
# The steering stubs are NOT a convenience: tests/test_hug_shore_bug2_wall_ahead.py
# drove HugShoreGoal.tick() into the real press-and-hold primitives, so running
# the unit suite STEERED THE LIVE SHIP.  They record calls rather than no-op
# silently, so a test that wants to assert on steering still can.

_BLANK_FRAME_SIZE = (2400, 1080)   # phone is fixed at 1080x2400 landscape


@pytest.fixture
def steering_calls():
    """Recorded (direction, duration_ms) from the stubbed hold primitives."""
    return _STEERING_LOG


_STEERING_LOG: list = []
_INPUT_LOG: list = []


@pytest.fixture
def input_calls():
    """Recorded (name, args, kwargs) from the stubbed adb_actions input primitives."""
    return _INPUT_LOG


@pytest.fixture(autouse=True)
def _offline_device(request, monkeypatch):
    if "device" in request.keywords:
        yield
        return

    _STEERING_LOG.clear()
    _INPUT_LOG.clear()

    try:
        from PIL import Image
        import capture.adb_capture as _cap
        blank = Image.new("RGB", _BLANK_FRAME_SIZE, (0, 0, 0))
        monkeypatch.setattr(_cap, "capture_screen", lambda *a, **kw: blank.copy(),
                            raising=False)
        monkeypatch.setattr(_cap, "_capture_screen_raw", lambda *a, **kw: blank.copy(),
                            raising=False)
    except Exception:
        pass

    # Stub the ADB READ, not the orientation logic above it — tests/test_orientation_guard.py
    # exercises `ensure_canonical_orientation` itself, so stubbing that function would test
    # the stub instead of the guard.
    try:
        import actions.orientation as _ori

        def _fake_adb_out(args):
            joined = " ".join(args)
            if "dumpsys" in joined:
                return "  mDisplayRotation=ROTATION_270\n  mDisplayId=0\n"
            if "accelerometer_rotation" in joined:
                return "0\n"
            return ""

        monkeypatch.setattr(_ori, "_adb_out", _fake_adb_out, raising=False)
    except Exception:
        pass

    # No unit test may send input. tests/test_village_recognition.py drove
    # SailToGoal.tick() into a real press_back() — and Back on the port overworld
    # raises the "Exit Game?" dialog, which is how the game got closed mid-session
    # once already. These record instead of firing, so tests can still assert intent.
    try:
        import actions.adb_actions as _adb_actions

        def _recorder(name):
            def fn(*a, **kw):
                _INPUT_LOG.append((name, a, kw))
                return None
            return fn

        for _fn in ("tap", "tap_fast", "long_press", "swipe", "swipe_fast",
                    "press_back", "wake", "pinch_zoom", "input_text"):
            monkeypatch.setattr(_adb_actions, _fn, _recorder(_fn), raising=False)
    except Exception:
        pass

    try:
        import actions.sea_actions as _sea

        def _stub_hold(name):
            def hold(duration_ms: int = 500, *a, **kw):
                _STEERING_LOG.append((name, duration_ms))
                return _sea.SeaActionResult(
                    ok=True, action=f"hold_{name}",
                    detail="stubbed by tests/conftest.py (offline)",
                    icon_state=None,
                )
            return hold

        monkeypatch.setattr(_sea, "hold_left", _stub_hold("left"), raising=False)
        monkeypatch.setattr(_sea, "hold_right", _stub_hold("right"), raising=False)
    except Exception:
        pass

    yield


# ── Per-test cache isolation ─────────────────────────────────────────────────
#
# Production frame-keyed caches leak between tests because every test builds a
# byte-identical blank frame, so they all hash to the same key.
#
# This was not theoretical: `_OBSTRUCTION_NONE_CACHE` (5s TTL) made
# tests/test_interruptor_structural.py order-dependent. The first test to run a
# blank frame through `classify_obstruction` cached "no obstruction here", and every
# later test using the same blank frame short-circuited before the keyword loop —
# so `_detect_interruptors` returned [] and four tests failed, while each passed
# alone. Cache-hit-on-identical-input is correct behaviour in production; it just
# has to be reset between tests.
# Session-level suppression globals: same leak, different shape. Once
# `_has_daily_news_close_x` records a Moondream "NO", it short-circuits every later
# call for an hour — across tests, that means whichever daily-news test runs first
# silently decides the rest. Reset to the module's own initial value.
_SESSION_GLOBALS = (
    ("brain.perceive", "_daily_news_no_suppress_until", 0.0),
)

_FRAME_KEYED_CACHES = (
    ("brain.perceive", "_OBSTRUCTION_NONE_CACHE"),
    ("vision.omniparser", "_FRAME_CACHE"),
    ("vision.moondream_cache", "_CACHE"),
    ("actions.sail_actions", "_OCR_CACHE"),
)


# Persisted facts: same leak, but it survives the PROCESS, not just the test.
# `memory/observed_facts` writes to memory/knowledge/state/observed_facts.json, so a test
# that starts a mission leaves a real record on disk — and `run_barter_command` then
# short-circuits into its resume path instead of planning. Live 2026-08-23 a stray
# `Apache Village / Camas / sailing_route` record made five TestOverloadedHold tests fail in
# the suite while every one of them passed alone. Redirect the store to a tmp file and empty
# it for each test: nothing under test should read or write the developer's real state.
# Live ENTRY PROBES are device work, and the "unit tests never touch the phone" rule covers
# them too. `brain.barter_command._try_barter_here` perceives the screen to decide whether the
# task can be joined at the barter step; under test that means a full perception pass per
# call — OmniParser, Qwen, Moondream — on a blank frame, which turned the barter suite from
# 0.3s into a hang. Stubbed to "not here", so the ordinary check-and-plan path is what unit
# tests exercise. A test that wants the probe patches it back explicitly.
@pytest.fixture(autouse=True)
def _no_live_entry_probe(request, monkeypatch):
    # A test that is ABOUT the probe marks itself `@pytest.mark.live_entry_probe` and gets
    # the real function (with its collaborators patched); everything else gets the stub.
    if request.node.get_closest_marker("live_entry_probe"):
        yield
        return
    try:
        from brain import barter_command
    except Exception:
        yield
        return
    monkeypatch.setattr(barter_command, "_try_barter_here", lambda _cmd: None,
                        raising=False)
    yield


@pytest.fixture(autouse=True)
def _isolate_observed_facts(tmp_path, monkeypatch):
    try:
        from memory import observed_facts
    except Exception:
        yield
        return
    monkeypatch.setattr(observed_facts, "_PATH", tmp_path / "observed_facts.json")
    observed_facts._reset_for_tests()
    yield
    observed_facts._reset_for_tests()


@pytest.fixture(autouse=True)
def _clear_frame_caches():
    import importlib

    def _clear():
        for mod_name, attr in _FRAME_KEYED_CACHES:
            mod = sys.modules.get(mod_name)
            if mod is None:
                continue
            cache = getattr(mod, attr, None)
            if isinstance(cache, dict):
                cache.clear()
        # The Moondream family cache decides port_overworld-vs-sea and is explicitly
        # session-scoped with a TTL, so a verdict set by one test silently answers for
        # the next — the classifier then skips the Moondream call entirely.
        _mfc = sys.modules.get("brain.moondream_family_cache")
        if _mfc is not None and hasattr(_mfc, "reset"):
            try:
                _mfc.reset()
            except Exception:
                pass
        for mod_name, attr, initial in _SESSION_GLOBALS:
            mod = sys.modules.get(mod_name)
            if mod is not None and hasattr(mod, attr):
                setattr(mod, attr, initial)

    _clear()
    yield
    _clear()


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_entry_probe: exercise brain.barter_command._try_barter_here for real "
        "(its collaborators must still be patched — it perceives the screen)")
