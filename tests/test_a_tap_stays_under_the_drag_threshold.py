# tests/test_a_tap_stays_under_the_drag_threshold.py
#
# A tap is issued as `input swipe` on purpose — an instantaneous, pixel-perfect
# `input tap` is what anti-cheat SDKs fingerprint. That is right, and it is kept.
#
# What was wrong was the SHAPE of the drift. Each axis drew independently in [-4,4],
# so both could take their maximum at once and the finger travelled 5.66px. An app
# that calls anything past ~5px a DRAG rather than a click then never fires the
# control, and the gesture does nothing at all — no press flash, no selection, no
# error, nothing to see in a screenshot.
#
# Measured live 2026-09-02 (trace_barter_cmd_2026-09-02T11-59-27): 3 of 62 taps
# produced ZERO pixel change against a median of 1.37M — 4.8%, against 4.9%
# predicted for the four corner draws. Two were the world map's search box, so the
# keyboard never opened and 'Trip' never reached it; one was the Candle tile at
# Tripoli, so the cart stayed empty, the Purchase button stayed greyed, and a
# gather leg ended two barter rounds short of its plan.
#
# The invariant is about DISPLACEMENT, not about either axis alone.

import math

import actions.adb_actions as adb


def test_the_finger_never_travels_far_enough_to_read_as_a_drag():
    """The whole defect in one assertion."""
    worst = max(math.hypot(*adb._drift_offset()) for _ in range(20000))
    # The old per-axis lattice reached 5.66px and lost ~1 tap in 20. Anything at or
    # near that is the bug back.
    assert worst < 5.0, f"a tap travelled {worst:.2f}px — the game reads that as a drag"
    assert worst <= adb.TAP_DRIFT_MAX + 1.0


def test_a_uniform_axis_draw_would_fail_this():
    """Pins WHY the shape matters, so the old form cannot come back looking harmless.

    Same maximum per axis, drawn independently — the form that shipped — puts 4.9%
    of taps past a 5px threshold."""
    import random
    lattice = [(random.randint(-4, 4), random.randint(-4, 4)) for _ in range(20000)]
    over = sum(1 for dx, dy in lattice if math.hypot(dx, dy) > 5.0)
    assert over / len(lattice) > 0.03          # ≈4.9%, and it matched the live 4.8%

    radial = [adb._drift_offset() for _ in range(20000)]
    assert not any(math.hypot(dx, dy) > 5.0 for dx, dy in radial)


def test_the_finger_still_moves():
    """The drift is the REASON this is a swipe — a fix that stopped it moving would
    pass the test above and reintroduce exactly what `input tap` was avoided for."""
    offsets = [adb._drift_offset() for _ in range(2000)]
    moved = sum(1 for dx, dy in offsets if (dx, dy) != (0, 0))
    assert moved > len(offsets) * 0.5, "a finger that never moves is the pixel-perfect tap again"
    # and it must not always move the SAME way
    assert len(set(offsets)) > 8


def test_the_press_duration_is_still_human_and_varied():
    assert adb.PRESS_DURATION_MIN_MS >= 60 and adb.PRESS_DURATION_MAX_MS <= 400
    assert adb.PRESS_DURATION_MAX_MS - adb.PRESS_DURATION_MIN_MS > 50, (
        "a near-constant press duration is itself a fingerprint"
    )
