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


# NO TEST MAY RUN FOR AN HOUR. `test_port_overworld_pre_sailing_is_NOT_arrival` ran 1h05m
# before anyone looked, because it drove a goal's tick() loop and every tick OCR'd a blank
# frame through ten navigation retries (2026-08-25). A test past this limit is not slow, it
# is stuck or is driving code it should be mocking — either way the run must SAY so rather
# than hang. Ten minutes sits far above any legitimate test here; the slowest real one
# measures ~76s.
TEST_TIMEOUT_S = 600


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
    config.addinivalue_line(
        "markers",
        "simulation: drives a goal's tick() loop — a simulated RUN, not a unit test; "
        "deselected unless --simulation")
    config.addinivalue_line(
        "markers",
        "stage_perception: re-perceives every stage-suite frame (~60s each); deselected "
        "unless --stage-perception. The FAST stage tests always run.")


def pytest_addoption(parser):
    parser.addoption("--device", action="store_true", default=False,
                     help="run tests marked @pytest.mark.device against the real phone")
    parser.addoption("--simulation", action="store_true", default=False,
                     help="run tests marked @pytest.mark.simulation (slow goal-loop runs)")
    parser.addoption("--stage-perception", action="store_true", default=False,
                     help="re-perceive every stage-suite frame and check the reading still "
                          "holds (~60s a frame — run it after touching perception)")
    parser.addoption("--test-timeout", type=int, default=TEST_TIMEOUT_S,
                     help=f"hard per-test time limit in seconds (default {TEST_TIMEOUT_S})")


def pytest_collection_modifyitems(config, items):
    # SIMULATED RUNS ARE NOT UNIT TESTS. A test that drives a goal's tick() loop executes the
    # real action code — navigation, OCR, retries — against whatever frame the fixtures hand
    # it, which for `capture_screen` is a BLANK image. `test_port_overworld_pre_sailing_is_
    # NOT_arrival` cost 1h05m that way and could only ever go red by raising from the action
    # path (2026-08-25). Until such a test is given real frames and a stated expectation for
    # each step, it belongs behind an opt-in rather than in every run.
    if not config.getoption("--simulation"):
        skip_sim = pytest.mark.skip(
            reason="simulated goal-loop run; needs real frames and stated expectations — "
                   "see docs/simulation_tests.md. Pass --simulation to run.")
        for item in items:
            if "simulation" in item.keywords:
                item.add_marker(skip_sim)

    # THE STAGE SUITE IS TWO LAYERS AND ONLY THE SLOW ONE IS OPT-IN. Asserting the DECISION
    # a frame leads to costs milliseconds and runs always. Re-PERCEIVING the frame costs ~60s,
    # because a frame no fingerprint settles falls through to Qwen — worth paying after a
    # change to perception, and not on every run. See tests/test_stages.py.
    if not config.getoption("--stage-perception"):
        skip_percep = pytest.mark.skip(
            reason="re-perceives stage frames (~60s each); pass --stage-perception to run")
        for item in items:
            if "stage_perception" in item.keywords:
                item.add_marker(skip_percep)

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
                # REPORT IT, as the real primitive does. The perceive repository invalidates
                # on actions, so a recorder that stays silent leaves it holding a frame from
                # before the tap — and a read-back loop would see an unchanged panel for ever.
                # Recording instead of firing must not also mean recording instead of SAYING.
                try:
                    _adb_actions._acted(name, 0.0)
                except Exception:
                    pass
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
    # Takes `position` too: the startup bootstrap now hands down what it established rather
    # than every caller re-establishing it (the second pass cost 9 captures and 84 seconds
    # before the first tap, live 2026-08-30).
    monkeypatch.setattr(barter_command, "_try_barter_here",
                        lambda _cmd, position=None: None, raising=False)
    yield


@pytest.fixture(autouse=True)
def _reset_the_perceive_repository():
    """A fresh observation per test.

    `actions.perception.screen()` is process-wide by design — the whole point is that two
    readers in one moment see the same frame. Across TESTS that same property is pollution:
    one test's held frame, generation and validity would be another's starting state. Same
    lesson as `_DAILY_NEWS_CLOSE_SEEN` (found by bisection 2026-08-31), applied before it
    can cost anything.
    """
    try:
        from actions import perception
    except Exception:
        yield
        return
    perception.reset_for_tests()
    yield
    perception.reset_for_tests()


@pytest.fixture(autouse=True)
def _reset_perceive_module_state():
    """Restore `brain.perceive`'s module-level mutable cells around every test.

    `_DAILY_NEWS_CLOSE_SEEN` is a one-element list holding the last daily-news close-X the bot
    saw, and `brain/perceive.py:1176` reads it when deciding how to dismiss. A test that fills
    it and does not put it back changes how LATER tests dismiss things.

    Found 2026-08-31 by bisection: `test_daily_news_is_found_not_assumed` sets the cell to
    (1695, 223) and leaves it, so `test_dismiss_handlers_typed::
    test_kb_position_takes_priority_over_dialog` PASSED alone and FAILED in the full suite —
    a failure that was neither a stale test nor a production bug, and which stood in the
    "known failures" list for as long as anyone remembers.

    Snapshot-and-restore here rather than in each mutator: the next test to touch a global
    should not have to know about this one.
    """
    try:
        import brain.perceive as _p
    except Exception:
        yield
        return
    saved = list(getattr(_p, "_DAILY_NEWS_CLOSE_SEEN", [None]))
    yield
    try:
        _p._DAILY_NEWS_CLOSE_SEEN[:] = saved
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _no_live_position_bootstrap(request, monkeypatch):
    """`run_barter_command` establishes position before taking a work order, and doing so
    PERCEIVES — which under these offline stand-ins means classifying a blank frame, up to
    12 ticks of model inference, per test.

    Added 2026-08-28 and immediately hung `test_barter_command.py`: 15 tests passed and the
    16th stalled. It is the failure CLAUDE.md already records — "left un-injected, a unit
    test reaches for the device... 18 tests went 0.2s -> 66s the moment `_what_it_saw` was
    added un-injected" — repeated in the same file.

    A test ABOUT the bootstrap marks itself `@pytest.mark.live_bootstrap` and gets the real
    thing; everything else is told it is at a port, which is where a run normally starts.
    """
    if request.node.get_closest_marker("live_bootstrap"):
        yield
        return
    try:
        from brain import barter_command
        from brain.activities import bootstrap
    except Exception:
        yield
        return
    stub = lambda **_kw: {"ok": True, "state": "port_overworld", "port": None}
    monkeypatch.setattr(bootstrap, "establish_position", stub, raising=False)
    monkeypatch.setattr(barter_command, "establish_position", stub, raising=False)
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



@pytest.fixture(autouse=True)
def _hard_time_limit(request):
    """Fail any test that runs past the limit, instead of letting it hang the suite."""
    import signal

    limit = request.config.getoption("--test-timeout")
    if not limit or not hasattr(signal, "SIGALRM"):
        yield
        return

    def _fire(_signum, _frame):
        raise TimeoutError(
            f"test exceeded the {limit}s limit — it is stuck, or it is driving real action "
            "code (see docs/simulation_tests.md). Raise --test-timeout only if the test "
            "genuinely needs longer.")

    previous = signal.signal(signal.SIGALRM, _fire)
    signal.alarm(limit)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


@pytest.fixture(scope="session", autouse=True)
def _load_the_action_modules_before_anything_is_patched():
    """Import the heavy action modules once, up front.

    `actions/sail_actions.py` binds `from capture.adb_capture import capture_screen` at module
    level. If that module happens to be imported for the FIRST time inside a test that has
    `capture.adb_capture.capture_screen` patched, the binding captures the MOCK — and
    `mock.patch` restores the module it patched, not the copy the fresh import took. The mock
    then answers every later call in the process.

    It surfaced as `test_run_goal` dying on `'object' object has no attribute 'width'`, three
    files away, in a test that passes cleanly on its own. Nothing about the leak points at the
    test that caused it, and which test that is depends on import ORDER — so it moves whenever
    an unrelated import is added or removed. Loading these first makes the order irrelevant.
    """
    for name in ("actions.sail_actions", "actions.fleet_status", "actions.adb_actions",
                 "capture.adb_capture"):
        try:
            __import__(name)
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _forget_the_last_frame():
    """Clear `brain.perceive`'s frame cache between tests.

    perceive keeps the last frame and its result in module globals, so it can skip a full
    pass when the screen has not changed. That state outlives a test: `test_barter_command`
    stubs `capture_screen` with a bare `object()`, and the next test to perceive compared its
    real frame against that object and died on `.width` — three files away, in a test that
    passes cleanly on its own.

    Same shape as the OmniParser singleton leak: a process-global filled under a stub. Left
    to accumulate, these make suite results depend on file order, which is how a green run
    stops meaning anything.
    """
    yield
    try:
        import brain.perceive as _p
        _p._PERCEIVE_LAST_FRAME = None
        _p._PERCEIVE_LAST_RESULT = None
        _p._PERCEIVE_CACHE_HITS = 0
    except Exception:
        pass

# ── OCR of a BLANK frame is empty; say so instead of asking a model ──────────
#
# `_offline_device` hands every offline test a blank 2400x1080 image, and code under test
# reads it with EasyOCR. Measured 2026-09-02 on the suite's slowest test: 91.6 of its 98.3
# seconds were EasyOCR — ONE HUNDRED `readtext` calls on that blank frame, because
# `open_world_map` polls ten times an attempt for a world map a blank image will never show.
# 83s of it was `Tensor.cpu()` stalling on MPS, 0.41s a call.
#
# The answer for an all-black image is "no text" whatever asks, so returning that directly is
# FAITHFUL, not a stub of convenience: same answer, none of the cost. That file went from
# 100.3s to 2.8s.
#
# Anything with actual pixels — every `tests/functional` frame — goes to the real reader
# untouched, which is what keeps this from hiding a reading bug.
_REAL_GET_READER = None


def _blank(image) -> bool:
    try:
        import numpy as np
        arr = np.asarray(image)
        return bool(arr.size) and not arr.any()
    except Exception:
        return False


class _ReaderThatSkipsBlankFrames:
    def __init__(self, inner):
        self._inner = inner

    def readtext(self, image, *a, **kw):
        if _blank(image):
            return []
        return self._inner.readtext(image, *a, **kw)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.fixture(scope="session", autouse=True)
def _no_ocr_on_a_blank_frame():
    """Wrap the OCR reader once for the session. See the note above."""
    global _REAL_GET_READER
    try:
        import vision.ocr as _ocr
    except Exception:
        yield
        return
    _REAL_GET_READER = _ocr._get_reader
    wrapped = {}

    def _get(*a, **kw):
        if "r" not in wrapped:
            wrapped["r"] = _ReaderThatSkipsBlankFrames(_REAL_GET_READER(*a, **kw))
        return wrapped["r"]

    _ocr._get_reader = _get
    try:
        yield
    finally:
        _ocr._get_reader = _REAL_GET_READER

# ── Sleeping advances the CLOCK, not the wall ────────────────────────────────
#
# Measured 2026-09-02: five files spent 365 seconds of the suite inside `time.sleep` — six
# minutes of a nineteen-minute run, in 37 calls. The same five run in 26s when the waiting is
# removed. Production has every right to those sleeps (a walk across a port, a settle after a
# tap, anti-cheat jitter); a test has no reason to sit through them.
#
# A VIRTUAL CLOCK, NOT A NO-OP. Nine test files turn on time actually passing — a 5s
# obstruction-cache TTL, `mission_progress`'s six-hour staleness, the polled waits — and a
# sleep that did nothing would silently break every one of them. So a sleep ADVANCES the
# clock: `monotonic`, `time` and `perf_counter` all move by what was slept, and nothing waits.
# Duration-dependent logic sees exactly what it would have seen.
#
# `device`-marked tests keep the real clock: they drive an actual phone, where waiting is the
# point.
@pytest.fixture(autouse=True)
def _sleep_advances_the_clock(request, monkeypatch):
    if "device" in request.keywords:
        yield
        return
    import time as _t

    slept = {"s": 0.0}
    real_monotonic, real_time, real_perf = _t.monotonic, _t.time, _t.perf_counter

    def _sleep(seconds=0.0, *_a, **_kw):
        try:
            slept["s"] += max(0.0, float(seconds or 0))
        except (TypeError, ValueError):
            pass

    monkeypatch.setattr(_t, "sleep", _sleep)
    monkeypatch.setattr(_t, "monotonic", lambda: real_monotonic() + slept["s"])
    monkeypatch.setattr(_t, "time", lambda: real_time() + slept["s"])
    monkeypatch.setattr(_t, "perf_counter", lambda: real_perf() + slept["s"])

    # `brain.plan_loop` does `from time import monotonic`, binding the real function at import
    # so the patch above cannot reach it. Left alone it would read a clock that never moved
    # while everything else read one that did.
    try:
        import brain.plan_loop as _pl
        monkeypatch.setattr(_pl, "monotonic", lambda: real_monotonic() + slept["s"],
                            raising=False)
    except Exception:
        pass
    yield

