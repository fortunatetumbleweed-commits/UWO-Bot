"""Tests for brain.dismissal_telemetry."""
import unittest

from brain import dismissal_telemetry as t


class DismissalTelemetryTests(unittest.TestCase):

    def setUp(self):
        t.reset()

    def test_records_per_handler_per_path(self):
        t.record("dismiss_tap_ok",        "typed")
        t.record("dismiss_tap_ok",        "typed")
        t.record("dismiss_tap_ok",        "legacy")
        t.record("dismiss_close_button",  "legacy")
        snap = t.snapshot()
        self.assertEqual(snap["dismiss_tap_ok"]["typed"], 2)
        self.assertEqual(snap["dismiss_tap_ok"]["legacy"], 1)
        self.assertEqual(snap["dismiss_close_button"]["legacy"], 1)

    def test_summary_table_format(self):
        t.record("dismiss_tap_ok", "typed")
        t.record("dismiss_tap_ok", "typed")
        t.record("dismiss_tap_ok", "legacy")
        t.record("dismiss_tap_ok", "noop")
        out = t.format_summary()
        # Header and dismiss_tap_ok row both present
        self.assertIn("handler", out)
        self.assertIn("typed%", out)
        self.assertIn("dismiss_tap_ok", out)
        # 2 typed / (2 typed + 1 legacy) = 66.7% (noop excluded from rate denom)
        self.assertIn("66.7%", out)

    def test_empty_summary_returns_empty_string(self):
        self.assertEqual(t.format_summary(), "")

    def test_reset_clears_counters(self):
        t.record("dismiss_tap_ok", "typed")
        t.reset()
        self.assertEqual(t.format_summary(), "")


if __name__ == "__main__":
    unittest.main()
