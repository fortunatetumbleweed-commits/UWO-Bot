"""Ground-truth sale verification — a sell counts as done when the world confirms
it (cargo dropped / ducats rose), not when a scripted dialog matches."""
import unittest
from unittest import mock

import actions.market_actions as m


class VerifySaleCompletedTests(unittest.TestCase):
    def _run(self, cargo_before, ducats_before, used_after, ducats_after):
        with mock.patch.object(m, "capture_screen", return_value=object()), \
             mock.patch.object(m, "_read_cargo_capacity", return_value=(used_after, 4108)), \
             mock.patch.object(m, "_read_ducats_safe", return_value=ducats_after):
            return m._verify_sale_completed("Amsterdam", cargo_before, ducats_before,
                                            good_names=["Whisky", "Steel"])

    def test_ducats_rose_is_verified_with_profit(self):
        # cargo counter flaky, but ducats went up → sold; profit = delta
        out = self._run(cargo_before=402, ducats_before=1_000_000,
                        used_after=556, ducats_after=1_103_034)
        self.assertIsNotNone(out)
        self.assertEqual(out[0].profit, 103_034)
        self.assertEqual(out[0].total_amount, 103_034)

    def test_cargo_drop_is_verified_even_without_ducats(self):
        # ducats unreadable (None) but cargo clearly dropped → sold (profit 0)
        out = self._run(cargo_before=3056, ducats_before=None,
                        used_after=556, ducats_after=None)
        self.assertIsNotNone(out)
        self.assertEqual(out[0].profit, 0)

    def test_no_change_returns_none(self):
        # neither cargo dropped nor ducats rose → genuinely not sold
        out = self._run(cargo_before=3056, ducats_before=1_000_000,
                        used_after=3056, ducats_after=1_000_000)
        self.assertIsNone(out)

    def test_tiny_cargo_wobble_not_counted_as_sale(self):
        # within the 10-unit noise band, and ducats flat → not a sale
        out = self._run(cargo_before=560, ducats_before=1_000_000,
                        used_after=556, ducats_after=1_000_000)
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
