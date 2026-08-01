"""Tests for the confirmation-required lat/lon acceptance gate.

The gate is a pure function — no OCR, no I/O.  Tests cover all four
cases the docstring promises plus the cascade-recovery scenario the
motivating bug came from.
"""
import unittest

from vision.sea_hud import (
    _NO_PENDING,
    accept_or_defer,
)


class FirstReadingTests(unittest.TestCase):
    """No prev → first reading of the voyage."""

    def test_first_reading_accepted_without_gating(self):
        accepted, pending = accept_or_defer(
            candidate=(30.14, 30.46), prev=None,
        )
        self.assertEqual(accepted, (30.14, 30.46))
        self.assertEqual(pending, _NO_PENDING)

    def test_no_candidate_returns_none(self):
        accepted, pending = accept_or_defer(
            candidate=None, prev=(30.0, 30.0),
        )
        self.assertIsNone(accepted)
        self.assertEqual(pending, _NO_PENDING)


class NormalCruiseTests(unittest.TestCase):
    """Candidate within threshold of prev → accept, reset pending."""

    def test_small_motion_accepted(self):
        accepted, pending = accept_or_defer(
            candidate=(30.5, 30.7), prev=(30.0, 30.5),
        )
        self.assertEqual(accepted, (30.5, 30.7))
        self.assertEqual(pending, _NO_PENDING)

    def test_acceptance_clears_existing_pending(self):
        """If a pending jump was in flight and a normal-motion read
        arrives, the pending state must reset so a future single
        wild read doesn't get accepted as a confirmation of the old
        pending value."""
        accepted, pending = accept_or_defer(
            candidate=(30.5, 30.7), prev=(30.0, 30.5),
            pending=((20.3, 61.5), 1),
        )
        self.assertEqual(accepted, (30.5, 30.7))
        self.assertEqual(pending, _NO_PENDING)


class JumpDeferralTests(unittest.TestCase):
    """Wild candidate → deferred until confirmed."""

    def test_first_wild_read_is_deferred(self):
        accepted, pending = accept_or_defer(
            candidate=(20.3, 61.5), prev=(20.78, 30.72),
        )
        self.assertIsNone(accepted)
        self.assertEqual(pending, ((20.3, 61.5), 1))

    def test_second_matching_wild_read_confirms_at_default_n2(self):
        """Default confirm_count = 2.  After a matching second read,
        accept and reset."""
        # First call: defer.
        accepted, pending = accept_or_defer(
            candidate=(20.3, 61.5), prev=(20.78, 30.72),
        )
        self.assertIsNone(accepted)
        # Second call with same wild value: confirmed.
        accepted, pending = accept_or_defer(
            candidate=(20.3, 61.5), prev=(20.78, 30.72),
            pending=pending,
        )
        self.assertEqual(accepted, (20.3, 61.5))
        self.assertEqual(pending, _NO_PENDING)

    def test_disagreeing_wild_read_resets_counter(self):
        """A second wild read that disagrees with the pending value
        starts a fresh confirmation cycle."""
        accepted, pending = accept_or_defer(
            candidate=(20.3, 61.5), prev=(20.78, 30.72),
        )
        # New different wild value — resets pending to itself.
        accepted, pending = accept_or_defer(
            candidate=(45.0, 70.0), prev=(20.78, 30.72),
            pending=pending,
        )
        self.assertIsNone(accepted)
        self.assertEqual(pending, ((45.0, 70.0), 1))


class CascadeRecoveryTests(unittest.TestCase):
    """The motivating bug: prev is bad (stuck-cached at a wild value),
    so all subsequent CORRECT readings look wild relative to prev.

    Confirmation count bounds the cascade to N ticks instead of 100+."""

    def test_stuck_bad_prev_recovers_after_n_confirmations(self):
        # prev is the bad cached value from the spike.
        bad_prev = (20.3, 61.5)
        # Real coast position.
        good = (20.78, 30.72)
        # First read: looks wild relative to bad_prev → defer.
        accepted, pending = accept_or_defer(
            candidate=good, prev=bad_prev,
        )
        self.assertIsNone(accepted)
        self.assertEqual(pending, (good, 1))
        # Second read of same good value: confirmed.
        accepted, pending = accept_or_defer(
            candidate=good, prev=bad_prev, pending=pending,
        )
        self.assertEqual(accepted, good)
        self.assertEqual(pending, _NO_PENDING)


class ThresholdAndCountKnobsTests(unittest.TestCase):
    """Both threshold_deg and confirm_count are tunable per call."""

    def test_threshold_relaxation_accepts_borderline(self):
        # 5° jump — beyond default 2° threshold, but accepted under
        # a relaxed 10° threshold.
        accepted, _ = accept_or_defer(
            candidate=(35.0, 35.0), prev=(30.0, 30.0),
            threshold_deg=10.0,
        )
        self.assertEqual(accepted, (35.0, 35.0))

    def test_confirm_count_of_three_requires_three_reads(self):
        # First two reads deferred; third accepts.
        pending = _NO_PENDING
        for _ in range(2):
            accepted, pending = accept_or_defer(
                candidate=(20.3, 61.5), prev=(20.78, 30.72),
                pending=pending, confirm_count=3,
            )
            self.assertIsNone(accepted)
        accepted, pending = accept_or_defer(
            candidate=(20.3, 61.5), prev=(20.78, 30.72),
            pending=pending, confirm_count=3,
        )
        self.assertEqual(accepted, (20.3, 61.5))


# ── Velocity-extrapolation path ─────────────────────────────────────────────

class VelocityExtrapolationTests(unittest.TestCase):
    """The velocity check rejects candidates that deviate from the
    linear-extrapolation prediction by more than
    `_VELOCITY_RESIDUAL_DEG`, even when the candidate sits well within
    the static jump threshold vs `prev`.

    Failure pattern this catches (from explore_port_20260606_230213):
      t44 lat=27.08  accepted
      t45 lat=27.0   misread (real ~27.59) — diff vs prev=0.08° is
                     inside the static 2° gate, so the OLD code
                     accepted; the new velocity check rejects because
                     extrapolation predicts ~27.59.
    """

    def test_velocity_check_rejects_misread_close_to_prev(self):
        # Build a clean trajectory heading north fast (mimics the
        # game's compressed time per tick — Δlat ≈ +0.51° / tick).
        history = [
            (27.08, 30.43, 44),
            (27.59, 30.50, 45),
        ]
        # t46 candidate (27.0, 30.55) — looks fine vs prev (diff 0.59°
        # < 2°), but extrapolation predicts (28.10, 30.57).  Residual
        # ~1.1°, well above 0.3° → reject.
        accepted, pending = accept_or_defer(
            candidate=(27.0, 30.55),
            prev=(27.59, 30.50),
            history=history,
            current_tick=46,
        )
        self.assertIsNone(accepted)
        # Pending is set (waiting for confirmation that this isn't a
        # one-tick OCR glitch).
        self.assertEqual(pending[0], (27.0, 30.55))
        self.assertEqual(pending[1], 1)

    def test_velocity_check_accepts_on_track_candidate(self):
        history = [
            (27.08, 30.43, 44),
            (27.59, 30.50, 45),
        ]
        # Continuing the same northbound rate → predicted t46 ≈
        # (28.10, 30.57).  A real reading near there should accept.
        accepted, pending = accept_or_defer(
            candidate=(28.05, 30.58),
            prev=(27.59, 30.50),
            history=history,
            current_tick=46,
        )
        self.assertEqual(accepted, (28.05, 30.58))
        self.assertEqual(pending, _NO_PENDING)

    def test_velocity_check_skipped_with_short_history(self):
        # Only one history entry — velocity undefined, fall back to
        # static check only.  Candidate within 2° of prev → accept.
        accepted, pending = accept_or_defer(
            candidate=(27.0, 30.55),
            prev=(27.08, 30.43),
            history=[(27.08, 30.43, 44)],
            current_tick=45,
        )
        self.assertEqual(accepted, (27.0, 30.55))

    def test_velocity_check_skipped_when_history_unset(self):
        # No history passed → behaviour identical to the original gate.
        accepted, pending = accept_or_defer(
            candidate=(27.0, 30.55),
            prev=(27.08, 30.43),
        )
        self.assertEqual(accepted, (27.0, 30.55))

    def test_confirm_breakout_after_two_velocity_rejections(self):
        """If the ship really is moving on a new trajectory (game day
        accelerated, course change), the gate must eventually accept
        instead of locking out forever.  Two consecutive matching
        wild reads confirm the new value — same path as the existing
        static-jump confirm logic."""
        history = [(27.08, 30.43, 44), (27.59, 30.50, 45)]
        # First wild read — defer.
        accepted, pending = accept_or_defer(
            candidate=(20.0, 30.55), prev=(27.59, 30.50),
            history=history, current_tick=46,
        )
        self.assertIsNone(accepted)
        # Second matching wild read — accepted (confirm_count=2).
        accepted, pending = accept_or_defer(
            candidate=(20.0, 30.60), prev=(27.59, 30.50),
            pending=pending,
            history=history, current_tick=47,
        )
        self.assertEqual(accepted, (20.0, 30.60))


if __name__ == "__main__":
    unittest.main()
