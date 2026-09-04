# Simulated runs vs unit tests

A test marked `@pytest.mark.simulation` drives a goal's `tick()` loop. That is not a unit
test — it executes the real action code (navigation, OCR, retries, sleeps) against whatever
frame the fixtures hand it. They are **deselected by default**:

    pytest                    # unit tests only
    pytest --simulation       # include the simulated runs

## Why they were taken out of the regular run

`test_port_overworld_pre_sailing_is_NOT_arrival` is the worked example. Its subject was real
and important: before SAILING the fleet is still at the source port, so a `port_overworld`
frame must not read as arrival, or the phase-aware fallback fires at the departure quay and
every voyage "arrives" before it starts.

It could not test that:

* it drove `tick()`, and for any phase below SAILING `tick()` falls *past* the arrival
  decision into the real departure action — `open_world_map()` OCRing conftest's BLANK frame
  through ten retries with sleeps;
* measured cost: **1h05m** for the class, 14 minutes of CPU, one tick alone over 2 minutes;
* its assertion could not fail on its merits — `SAILING = 8` and every phase it listed is
  1-7, so `action != "arrived"` was guaranteed by the enum ordering;
* the only way it could go red was by RAISING from the action path, which is what it had been
  doing, on conftest's adb guard — red for a reason unrelated to its subject, and unnoticed.

The tell was the runtime. **A unit test that takes an hour is not testing a unit.** The fix
was to test the decision (`arrival_verdict`) rather than the voyage: 12 seconds, and it now
covers three rules that had no test at all.

## The hard limit

No test may run longer than **10 minutes** (`TEST_TIMEOUT_S` in `tests/conftest.py`). Past
that it fails with a message rather than hanging the run:

    TimeoutError: test exceeded the 600s limit — it is stuck, or it is driving real
    action code (see docs/simulation_tests.md).

The limit is enforced per test by an autouse fixture (SIGALRM — no extra dependency), and can
be raised for one run with `--test-timeout=N`. Ten minutes sits far above any legitimate test
here: the slowest real one measures ~76s. The point is not to allow ten-minute tests, it is
that an hour must never again pass unnoticed.

## What a simulated run must provide to be worth running

A simulation is worth having — a decision can be right while the sequence around it is
wrong — but only if it is specified. Before re-enabling one, it needs all four:

1. **Real frames, not blanks.** `capture_screen` is stubbed with a blank image, so every
   perception call returns nothing and the code under test exercises its failure paths. A
   simulation must supply an actual frame per step, from `data/test_frames/`.
2. **A stated expectation per step.** Not just the final assertion: which action is expected
   at each tick, and why. Otherwise a run that reaches the right end by the wrong route
   passes.
3. **A bounded action surface.** Say explicitly what is mocked (the dispatcher, `clear_
   blockers`, the planner) and what is genuinely exercised. A test that accidentally runs
   `open_world_map` is measuring retry behaviour, not the goal.
4. **A runtime budget.** Seconds, not minutes. If it cannot meet that with real frames, it is
   an integration harness and belongs outside the test suite entirely.

Until a simulation meets those, it is asserting less than its name suggests while costing
more than the rest of the suite combined.

## Currently marked

    tests/test_sail_to_arrival.py            3
    tests/test_village_recognition.py        3
    tests/test_hug_shore_regression.py       1
    tests/test_lyapunov_point_pursuit.py     2
    tests/test_hug_shore_bug2_wall_ahead.py  6

None of these has been reviewed against the four requirements above; they were marked because
they drive `tick()`, which is a mechanical property, not a judgement about their value. Each
still needs the definition work — what it is for, and which frames make it mean something.
