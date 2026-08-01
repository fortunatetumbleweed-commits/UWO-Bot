"""
Tests for Fix D: cross-call dismissal no-op tracker.

The harbour run on 2026-05-04 looped six times dismissing the same
'harbor_supply_manual_dialog_dismiss' interruptor, each cycle ~30s,
because the dismissal action was a no-op (post-tap frame still showed
the same dialog).  The tracker breaks the loop after a configurable
number of consecutive no-ops on the same interruptor id.
"""

import unittest
from unittest.mock import patch, MagicMock

from PIL import Image

from brain import perceive as _p


def _frame() -> Image.Image:
    return Image.new("RGB", (240, 108), color=(0, 0, 0))


def _tokens(*words):
    return [(w, 0.9, 100, 50) for w in words]


class SignatureTests(unittest.TestCase):

    def test_signature_is_stable_for_same_tokens(self):
        a = _p._signature_for_dismissal(_tokens("Hello", "World"))
        b = _p._signature_for_dismissal(_tokens("World", "Hello"))
        self.assertEqual(a, b, "signature must be order-independent")

    def test_signature_differs_when_tokens_change(self):
        a = _p._signature_for_dismissal(_tokens("Hello", "World"))
        b = _p._signature_for_dismissal(_tokens("Hello", "Goodbye"))
        self.assertNotEqual(a, b)

    def test_reset_clears_counters(self):
        _p._DISMISSAL_NOOP_COUNTERS["foo"] = 5
        _p._DISMISSAL_LAST_SIGNATURE["foo"] = "x"
        _p._reset_dismissal_tracker("foo")
        self.assertNotIn("foo", _p._DISMISSAL_NOOP_COUNTERS)
        self.assertNotIn("foo", _p._DISMISSAL_LAST_SIGNATURE)


class DismissNoopTrackerTests(unittest.TestCase):
    """Verify the tracker counts and skips per-Fix-D semantics."""

    def setUp(self):
        _p._DISMISSAL_NOOP_COUNTERS.clear()
        _p._DISMISSAL_LAST_SIGNATURE.clear()

    def tearDown(self):
        _p._DISMISSAL_NOOP_COUNTERS.clear()
        _p._DISMISSAL_LAST_SIGNATURE.clear()

    def _patches(self, ocr_tokens_sequence, found_iids="stuck_dialog"):
        """Return a context-manager fixture that pretends:
          - capture_screen returns _frame() on every call
          - _ocr_frame returns each item from ocr_tokens_sequence in order
          - _detect_interruptors returns [found_iids] on first call, [] on
            subsequent calls (so the loop terminates)
        """
        sequence = list(ocr_tokens_sequence)
        ocr_call_count = {"n": 0}
        def fake_ocr(*a, **kw):
            i = ocr_call_count["n"]
            ocr_call_count["n"] += 1
            return sequence[min(i, len(sequence) - 1)]

        find_call_count = {"n": 0}
        def fake_detect(frame, tokens):
            find_call_count["n"] += 1
            iids = [found_iids] if find_call_count["n"] == 1 else []
            # Phase: _detect_interruptors now returns (iids, obstruction).
            # Tests don't need the obstruction so return None for it.
            return iids, None

        return patch.multiple(
            "brain.perceive",
            _detect_interruptors=fake_detect,
            _dismiss_interruptor=MagicMock(),
            _match_learned_recoveries=MagicMock(return_value=[]),
        ), patch("actions.sail_actions._ocr_frame", side_effect=fake_ocr), \
           patch("capture.adb_capture.capture_screen", return_value=_frame())

    def test_successful_dismissal_resets_tracker(self):
        """Pre-dismissal tokens differ from post-dismissal — counter stays at 0."""
        # First OCR call (pre-detect): "stuck dialog ok"
        # Second OCR call (post-dismiss in same iter): "back to normal"
        # Third OCR call (next loop iter — no interruptors found, exits)
        pre  = _tokens("stuck", "dialog", "ok")
        post = _tokens("back", "to", "normal")
        no_more = _tokens("nothing", "to", "see")
        a, b, c = self._patches([pre, post, no_more], "stuck_dialog")
        with a, b, c:
            _p.dismiss_interruptors(_frame())
        self.assertEqual(_p._DISMISSAL_NOOP_COUNTERS.get("stuck_dialog", 0), 0)

    def test_noop_dismissal_increments_counter(self):
        """Pre and post tokens identical → no-op detected, counter bumps."""
        same = _tokens("stuck", "dialog", "ok")
        a, b, c = self._patches([same, same, _tokens("nothing")], "stuck_dialog")
        with a, b, c:
            _p.dismiss_interruptors(_frame())
        self.assertEqual(_p._DISMISSAL_NOOP_COUNTERS.get("stuck_dialog", 0), 1)

    def test_skip_after_threshold(self):
        """After _NOOP_SKIP_THRESHOLD no-ops, the interruptor is skipped."""
        _p._DISMISSAL_NOOP_COUNTERS["stuck_dialog"] = _p._NOOP_SKIP_THRESHOLD
        same = _tokens("stuck", "dialog", "ok")
        a, b, c = self._patches([same, same, _tokens("nothing")], "stuck_dialog")
        dismiss_mock = MagicMock()
        with a, b, c, \
             patch("brain.perceive._dismiss_interruptor", dismiss_mock):
            _p.dismiss_interruptors(_frame())
        # Skipped — _dismiss_interruptor must NOT have been called for the
        # stuck_dialog because the tracker already hit the skip threshold.
        # Iterator may have called _dismiss for OTHER ids if any; here we
        # expect zero calls.
        dismiss_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
