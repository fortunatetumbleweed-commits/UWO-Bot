"""Tests for get_trade_point_award — perceive→tap-the-chest→verify-points-dropped.
The reward dialog is TRANSIENT (can vanish between ticks), so success is verified by the
STATE change: points fall below 1,000."""
import types
import unittest

from actions.market_actions import get_trade_point_award, _read_trade_points


def _counter(label, x1=145, y1=628, x2=449, y2=710):
    return types.SimpleNamespace(element_type="button", label=label,
                                 x1=x1, y1=y1, x2=x2, y2=y2,
                                 cx=(x1 + x2) // 2, cy=(y1 + y2) // 2)


class ReadTradePointsTests(unittest.TestCase):
    def test_parses_counter(self):
        pts, el = _read_trade_points([_counter("7,688/1,000")])
        self.assertEqual(pts, 7688)
        self.assertIsNotNone(el)

    def test_ignores_right_panel_and_noise(self):
        right = _counter("9,999/1,000", x1=1900, x2=2300)   # not the left panel
        pts, _ = _read_trade_points([right, _counter("hello")])
        self.assertIsNone(pts)


class ClaimTests(unittest.TestCase):
    def _run(self, reads, taps):
        """reads = successive element lists returned by omni_fn."""
        seq = {"i": 0}
        def omni(_f):
            i = min(seq["i"], len(reads) - 1); seq["i"] += 1
            return reads[i]
        return get_trade_point_award(
            capture_fn=lambda: "F", tap_fn=lambda x, y: taps.append((x, y)),
            omni_fn=omni, settle=0, max_verify=2)

    def test_claims_when_available_and_verifies_drop(self):
        taps = []
        res = self._run([[_counter("7,688/1,000")],      # perceive: award available
                         [_counter("688/1,000")]],        # verify: dropped below 1,000
                        taps)
        self.assertTrue(res["ok"] and res["claimed"])
        self.assertEqual((res["points_before"], res["points_after"]), (7688, 688))
        self.assertEqual(taps, [(449 - 49, (628 + 710) // 2)])   # chest at counter's right end

    def test_transient_dialog_occludes_then_verifies(self):
        # First verify tick can't read the counter (reward dialog on top) → keep polling.
        taps = []
        res = self._run([[_counter("1,204/1,000")], [], [_counter("204/1,000")]], taps)
        self.assertTrue(res["claimed"])

    def test_no_award_pending_is_noop(self):
        taps = []
        res = self._run([[_counter("425/1,000")]], taps)
        self.assertTrue(res["ok"]); self.assertFalse(res["claimed"])
        self.assertEqual(taps, [])                      # never taps without an award

    def test_counter_not_visible(self):
        res = self._run([[]], [])
        self.assertFalse(res["ok"]); self.assertFalse(res["claimed"])

    def test_unverified_claim_retries_once_then_reports_failure(self):
        # A tap can be swallowed (live 2026-08-20) → ONE verified re-tap, then escalate.
        taps = []
        res = self._run([[_counter("2,000/1,000")], [_counter("2,000/1,000")],
                         [_counter("2,000/1,000")]], taps)
        self.assertFalse(res["ok"])                     # points never dropped → not verified
        self.assertEqual(len(taps), 2)                  # initial tap + one retry, never more


if __name__ == "__main__":
    unittest.main()
