"""Tests for _sell_handle_negotiation_and_wait_result.

Regression: in live runs the sell flow's single-shot negotiation + wait_for_screen
pattern was reliably missing the result dialog after sells.  The new helper uses
the buy-flow loop pattern instead: result-check first each iteration, then
negotiation, repeat until result detected or timeout.
"""
import unittest
from unittest.mock import patch, MagicMock


def _fake_frame():
    f = MagicMock(); f.width = 2400; f.height = 1080; f.size = (2400, 1080)
    return f


class SellNegotiationLoopTests(unittest.TestCase):

    def _patches(self, screen_contains_side_effect):
        """Stack the patches each test needs."""
        return [
            patch("actions.market_actions.time.sleep"),
            patch("actions.market_actions.capture_screen",
                  return_value=_fake_frame()),
            patch("actions.market_actions._screen_contains",
                  side_effect=screen_contains_side_effect),
            patch("actions.market_actions._handle_negotiation"),
            patch("actions.market_actions._ckb_lazy",
                  return_value=("total amount",)),
        ]

    def test_result_appears_immediately_no_negotiation(self):
        """First poll sees result dialog directly."""
        from actions import market_actions as M
        # _screen_contains is called twice per iter: result-check, then
        # negotiation-check.  First call → True (result visible).
        calls = [True]
        with patch("actions.market_actions.time.sleep"), \
             patch("actions.market_actions.capture_screen",
                   return_value=_fake_frame()), \
             patch("actions.market_actions._screen_contains",
                   side_effect=lambda *a, **k: calls.pop(0) if calls else False), \
             patch("actions.market_actions._handle_negotiation"), \
             patch("actions.market_actions._ckb_lazy",
                   return_value=("total amount",)), \
             patch("brain.kb.control") as mock_ckb:
            mock_ckb.return_value.dialog_confirmation.return_value = ("negotiat",)
            rounds, frame = M._sell_handle_negotiation_and_wait_result({}, strategy="no")
            self.assertEqual(rounds, 0)
            self.assertIsNotNone(frame)

    def test_one_negotiation_round_then_result(self):
        """First iteration: no result, sees negotiation, taps Skip; second
        iteration after tap: result visible."""
        from actions import market_actions as M
        # Call order:
        # iter 1: result-check(False), negotiation-check(True),
        #         after-tap result-check(True) ← returns
        responses = [False, True, True]
        with patch("actions.market_actions.time.sleep"), \
             patch("actions.market_actions.capture_screen",
                   return_value=_fake_frame()), \
             patch("actions.market_actions._screen_contains",
                   side_effect=lambda *a, **k: responses.pop(0) if responses else False), \
             patch("actions.market_actions._handle_negotiation") as mock_handle, \
             patch("actions.market_actions._ckb_lazy",
                   return_value=("total amount",)), \
             patch("brain.kb.control") as mock_ckb:
            mock_ckb.return_value.dialog_confirmation.return_value = ("negotiat",)
            rounds, frame = M._sell_handle_negotiation_and_wait_result({}, strategy="no")
            self.assertEqual(rounds, 1)
            self.assertIsNotNone(frame)
            mock_handle.assert_called_once()

    def test_multiple_negotiation_rounds(self):
        """Result appears only after the second negotiation round.
        Each loop iteration calls _screen_contains twice (result + negotiation)
        plus once after the Skip tap.
        """
        from actions import market_actions as M
        # iter 1: result(False) negotiation(True) after-tap-result(False)
        # iter 2: result(False) negotiation(True) after-tap-result(True)
        responses = [False, True, False,
                     False, True, True]
        with patch("actions.market_actions.time.sleep"), \
             patch("actions.market_actions.capture_screen",
                   return_value=_fake_frame()), \
             patch("actions.market_actions._screen_contains",
                   side_effect=lambda *a, **k: responses.pop(0) if responses else False), \
             patch("actions.market_actions._handle_negotiation") as mock_handle, \
             patch("actions.market_actions._ckb_lazy",
                   return_value=("total amount",)), \
             patch("brain.kb.control") as mock_ckb:
            mock_ckb.return_value.dialog_confirmation.return_value = ("negotiat",)
            rounds, frame = M._sell_handle_negotiation_and_wait_result({}, strategy="no")
            self.assertEqual(rounds, 2)
            self.assertIsNotNone(frame)
            self.assertEqual(mock_handle.call_count, 2)

    def test_neither_dialog_for_3_polls_returns_none(self):
        """If neither dialog appears for 3 consecutive polls, give up."""
        from actions import market_actions as M
        # Every _screen_contains returns False
        with patch("actions.market_actions.time.sleep"), \
             patch("actions.market_actions.capture_screen",
                   return_value=_fake_frame()), \
             patch("actions.market_actions._screen_contains",
                   return_value=False), \
             patch("actions.market_actions._handle_negotiation") as mock_handle, \
             patch("actions.market_actions._ckb_lazy",
                   return_value=("total amount",)), \
             patch("brain.kb.control") as mock_ckb:
            mock_ckb.return_value.dialog_confirmation.return_value = ("negotiat",)
            rounds, frame = M._sell_handle_negotiation_and_wait_result({}, strategy="no")
            self.assertIsNone(frame)
            # rounds may be 1-3 depending on counter timing
            self.assertLessEqual(rounds, 3)
            mock_handle.assert_not_called()

    def test_max_rounds_safety_cap(self):
        """Endless negotiation rounds — loop stops at max_rounds."""
        from actions import market_actions as M
        # Always negotiation, never result, never escape
        def alternating(*a, **k):
            # result-check False, negotiation True, after-tap-result False
            # repeated forever
            if not hasattr(alternating, 'i'):
                alternating.i = 0
            alternating.i += 1
            # pattern: F, T, F, F, T, F, ...
            return alternating.i % 3 == 2
        with patch("actions.market_actions.time.sleep"), \
             patch("actions.market_actions.capture_screen",
                   return_value=_fake_frame()), \
             patch("actions.market_actions._screen_contains",
                   side_effect=alternating), \
             patch("actions.market_actions._handle_negotiation") as mock_handle, \
             patch("actions.market_actions._ckb_lazy",
                   return_value=("total amount",)), \
             patch("brain.kb.control") as mock_ckb:
            mock_ckb.return_value.dialog_confirmation.return_value = ("negotiat",)
            rounds, frame = M._sell_handle_negotiation_and_wait_result(
                {}, strategy="no", max_rounds=4,
            )
            self.assertIsNone(frame)
            self.assertEqual(rounds, 4)


if __name__ == "__main__":
    unittest.main()
