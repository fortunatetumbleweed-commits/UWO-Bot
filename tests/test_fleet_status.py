"""Main-menu fleet reader + the village-leg supply gate.

Device layer fully mocked. The behaviour that matters here is the SAFE-SIDE one: an
unreadable supply must never pass the gate (villages cannot resupply — the 2026-08-20
fleet death happened on a leg nobody checked)."""
import types
import unittest
from unittest import mock

from actions.fleet_status import read_fleet_status, verify_supply_for_leg


def _patch(loc_seq, elements):
    """Patch the device/perception seams read_fleet_status uses."""
    locs = iter(loc_seq)
    return [
        mock.patch("capture.adb_capture.capture_screen", return_value=object()),
        mock.patch("actions.sail_actions.where_am_i",
                   side_effect=lambda *_a, **_k: {"location": next(locs)}),
        mock.patch("vision.omniparser.parse_fast_cached", return_value=elements),
        mock.patch("actions.adb_actions.tap"),
        mock.patch("actions.adb_actions.press_back"),
        mock.patch("time.sleep"),
    ]


class ReadFleetStatusTests(unittest.TestCase):
    def _run(self, loc_seq, supply=(329, 329), cargo=(566, 4108)):
        patches = _patch(loc_seq, [])
        with mock.patch("vision.hud_readers.read_supply", return_value=supply), \
             mock.patch("vision.hud_readers.read_cargo", return_value=cargo):
            for p in patches:
                p.start()
            try:
                return read_fleet_status(attempts=1)
            finally:
                for p in patches:
                    p.stop()

    def test_opens_the_menu_from_the_overworld_and_reads_both(self):
        out = self._run(["port_overworld", "main_menu"])
        self.assertTrue(out["ok"])
        self.assertEqual((out["water"], out["food"]), (329, 329))
        self.assertEqual(out["cargo_capacity"], 4108)
        self.assertEqual(out["supply_days"], 12.0)          # 329 / 27.42 per day

    def test_already_on_the_main_menu_needs_no_tap(self):
        out = self._run(["main_menu"])
        self.assertTrue(out["ok"])

    def test_refuses_to_tap_the_corner_from_a_screen_with_no_hamburger(self):
        out = self._run(["market"])
        self.assertFalse(out["ok"])
        self.assertIn("no ☰", out["reason"])

    def test_menu_that_did_not_open_is_a_structured_failure(self):
        out = self._run(["port_overworld", "port_overworld"])
        self.assertFalse(out["ok"])
        self.assertIn("did not open", out["reason"])

    def test_nothing_readable_is_reported_not_defaulted(self):
        out = self._run(["main_menu"], supply=None, cargo=None)
        self.assertFalse(out["ok"])
        self.assertIsNone(out["supply_days"])


class SupplyGateTests(unittest.TestCase):
    def test_enough_supply_passes(self):
        res = verify_supply_for_leg(7.0, status={"supply_days": 12.0})
        self.assertTrue(res["ok"])

    def test_short_supply_fails_with_the_numbers(self):
        res = verify_supply_for_leg(7.0, status={"supply_days": 3.1})
        self.assertFalse(res["ok"])
        self.assertIn("3.1", res["reason"])
        self.assertIn("7.0", res["reason"])

    def test_unreadable_supply_is_a_FAIL_not_a_pass(self):
        res = verify_supply_for_leg(7.0, status={"supply_days": None,
                                                 "reason": "main menu did not open"})
        self.assertFalse(res["ok"])
        self.assertIn("unreadable", res["reason"])

    def test_exactly_enough_passes(self):
        self.assertTrue(verify_supply_for_leg(7.0, status={"supply_days": 7.0})["ok"])


if __name__ == "__main__":
    unittest.main()


# NOTE: the old HamburgerDetectionTests asserted "picks the rightmost icon in the top
# band". That WAS the bug — it tapped chat, then mail, while logging "the ☰ was detected"
# (live 2026-08-21). Identity-based location is covered by tests/test_menu_icon_identity.py.


class SupplyMisreadRegressionTests(unittest.TestCase):
    """Live 2026-08-21: the main menu read water=food=4108 (149.8 days) for a fleet that
    actually held ZERO of each. The panel has no numeric element beside Water/Food, so
    'nearest number' reached one row up and took the Total Load Capacity pair. That read
    would have passed the 7-day village gate and sailed an unsupplied fleet."""

    def _main_menu_elements(self):
        # The real geometry from frame_0017 of trace_barter_cmd_2026-08-21T12-59-46.
        return [
            types.SimpleNamespace(element_type="text", label="Total Load Capacity",
                                  x1=29, y1=457, x2=299, y2=495, cx=164, cy=476),
            types.SimpleNamespace(element_type="text", label="57/4,108",
                                  x1=500, y1=455, x2=622, y2=493, cx=561, cy=474),
            types.SimpleNamespace(element_type="button", label="Water",
                                  x1=27, y1=491, x2=346, y2=544, cx=186, cy=517),
            types.SimpleNamespace(element_type="button", label="Food",
                                  x1=350, y1=491, x2=666, y2=545, cx=508, cy=518),
        ]

    def test_the_capacity_pair_can_no_longer_masquerade_as_supply(self):
        from vision.hud_readers import read_supply
        # 57/4,108 sits 43px above the Water row — a different row, so not its value.
        self.assertIsNone(read_supply(self._main_menu_elements()))

    def test_the_tile_reader_returns_the_true_zero(self):
        from vision.hud_readers import read_supply_rows
        frame = types.SimpleNamespace(width=2400, height=1080,
                                      crop=lambda box: types.SimpleNamespace(
                                          width=box[2] - box[0], height=box[3] - box[1],
                                          resize=lambda size: "img"))
        got = read_supply_rows(frame, self._main_menu_elements(), ocr_fn=lambda im: ["0"])
        self.assertEqual(got, (0, 0))

    def test_zero_supply_fails_the_village_gate(self):
        # The whole point: 0 days must NOT pass, where 149.8 days did.
        res = verify_supply_for_leg(7.0, status={"supply_days": 0.0})
        self.assertFalse(res["ok"])

    def test_the_crop_is_taken_from_the_elements_own_bbox(self):
        from vision.hud_readers import read_supply_rows
        boxes = []

        def _crop(box):
            boxes.append(box)
            return types.SimpleNamespace(width=box[2] - box[0], height=box[3] - box[1],
                                         resize=lambda size: "img")

        frame = types.SimpleNamespace(width=2400, height=1080, crop=_crop)
        read_supply_rows(frame, self._main_menu_elements(), ocr_fn=lambda im: ["0"])
        # Right-hand side of the Water tile (27..346), never a remembered coordinate.
        self.assertEqual(boxes[0], (170, 491, 346, 544))


class TransientOverlayRetryTests(unittest.TestCase):
    """NPC chatter bubbles drift across the port overworld. One sitting over the top-right
    icon strip hid the ☰ and made where_am_i report 'building', failing a read that would
    have worked seconds later (live 2026-08-21). Re-perceive, don't fail on one frame."""

    def test_a_transient_misclassification_is_retried(self):
        locs = iter(["building", "port_overworld", "main_menu"])
        with mock.patch("capture.adb_capture.capture_screen", return_value=object()), \
             mock.patch("actions.sail_actions.where_am_i",
                        side_effect=lambda *a, **k: {"location": next(locs)}), \
             mock.patch("vision.omniparser.parse_fast_cached", return_value=[]), \
             mock.patch("vision.hud_readers.read_supply_rows", return_value=(329, 329)), \
             mock.patch("vision.hud_readers.read_cargo", return_value=(57, 4108)), \
             mock.patch("actions.ui.tap_element", return_value=True), \
             mock.patch("actions.ui.tap_at"), mock.patch("actions.ui.back"), \
             mock.patch("actions.ui.settle"), \
             mock.patch("actions.ui.find_menu_icon", return_value=None):
            out = read_fleet_status(attempts=3)
        self.assertTrue(out["ok"], out["reason"])
        self.assertEqual(out["cargo_capacity"], 4108)

    def test_it_still_gives_up_with_a_reason(self):
        with mock.patch("capture.adb_capture.capture_screen", return_value=object()), \
             mock.patch("actions.sail_actions.where_am_i",
                        return_value={"location": "market"}), \
             mock.patch("actions.ui.settle"):
            out = read_fleet_status(attempts=2)
        self.assertFalse(out["ok"])
        self.assertIn("no ☰", out["reason"])


class SupplyGateLocationTests(unittest.TestCase):
    """Where the supply gate belongs (user 2026-08-21): supplies load at Supply
    Departure, so 0 supply IN PORT is normal and must not abort a mission. Days-of-supply
    is only meaningful AT SEA, where the HUD reports it."""

    def _verify(self, location, status=None):
        from brain.barter_mission_live import make_live_executors
        task = types.SimpleNamespace(params={"village": "Melanesian Village"})
        with mock.patch("actions.sail_actions.where_am_i",
                        return_value={"location": location}), \
             mock.patch("actions.fleet_status.verify_supply_for_leg",
                        return_value=status or {"ok": True, "reason": "supply 9d"}):
            return make_live_executors(object())["supply_verify"](task)

    def test_zero_supply_in_port_is_fine(self):
        # The exact case that would have blocked the Diu mission: 0 water, 0 food, at port.
        res = self._verify("port_overworld")
        self.assertTrue(res["ok"])
        self.assertIn("load at Supply Departure", res["reason"])

    def test_at_sea_it_does_gate_on_days(self):
        res = self._verify("sea", status={"ok": False, "reason": "supply 3.1d < 7.0d"})
        self.assertFalse(res["ok"])
        self.assertIn("3.1d", res["reason"])

    def test_somewhere_that_cannot_supply_is_refused(self):
        res = self._verify("market")
        self.assertFalse(res["ok"])
        self.assertIn("expected a port or open sea", res["reason"])


class VillageLegFloorTests(unittest.TestCase):
    """A village has no harbour, so its leg must carry the RETURN trip too — the at-sea
    watch gets an absolute floor, not just the one-way ETA plus a buffer."""

    def test_the_village_leg_passes_a_round_trip_floor(self):
        from brain.barter_mission_live import make_live_executors
        from brain.supply_planner import VILLAGE_LEG_RESERVE_DAYS
        seen = {}

        def _drive(dest, *, kind="port", resupply=True, min_supply_days=None, **kw):
            seen.update(dest=dest, floor=min_supply_days, kind=kind)
            return {"ok": True}

        task = types.SimpleNamespace(params={"village": "Melanesian Village"})
        with mock.patch("brain.barter_mission_live._sail_to", side_effect=_drive):
            make_live_executors(object())["sail_to_village"](task)
        self.assertEqual(seen["floor"], VILLAGE_LEG_RESERVE_DAYS)
        self.assertEqual(seen["kind"], "village", "villages are on the Explore tab")

    def test_the_floor_overrides_a_short_one_way_eta(self):
        from actions.task_runner import _ensure_supply
        # 4 days aboard covers a 1-day ETA + 2 buffer, but NOT a 7-day round trip.
        with mock.patch("actions.sail_actions.where_am_i",
                        return_value={"location": "sea", "port": None}), \
             mock.patch("actions.sail_actions.read_sea_hud",
                        return_value={"supply_days": 4, "eta_days": 1}), \
             mock.patch("brain.route_planner.plan_route", return_value=["A", "B"]):
            days = _ensure_supply("Melanesian Village", min_days=7.0)
        self.assertEqual(days, 4)     # reported back for the checkback cadence
