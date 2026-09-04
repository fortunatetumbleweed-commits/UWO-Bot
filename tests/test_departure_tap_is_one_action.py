"""Tapping the harbour's departure button is one action; the rest is the caller's.

`_depart_from_harbour` wrapped four things around a single tap: waiting out the loading
screens, re-navigating when the button was absent, and — twice — escalating into
`recover_to_port_overworld` when a blocker appeared.

That last one crosses WORLDS from inside a primitive, which is never a primitive's decision,
and it is one half of the `exit_to_overworld <-> recover_to_port_overworld` cycle: two
sub-loops able to re-enter each other with no shared budget between them.

`tap_supply_departure` taps and reports. A blocker is REPORTED, not recovered from.
"""

from __future__ import annotations

import unittest
from unittest import mock

from actions import sail_actions


class TapSupplyDeparture(unittest.TestCase):

    def _tap(self, button_result):
        with mock.patch.object(sail_actions, "_tap_depart_button",
                               return_value=button_result), \
             mock.patch.object(sail_actions, "capture_screen", lambda: object()):
            return sail_actions.tap_supply_departure()

    def test_a_successful_tap_is_reported_as_tapped(self):
        res = self._tap("tapped")
        self.assertTrue(res["tapped"])
        self.assertIsNone(res["blocked"])

    def test_tapped_does_not_claim_the_fleet_left(self):
        """`tapped` means the button was pressed. Whether the fleet sailed is perceived."""
        res = self._tap("tapped")
        self.assertNotIn("sea", str(res).lower())

    def test_a_missing_button_is_reported_not_retried(self):
        res = self._tap("not_found")
        self.assertFalse(res["tapped"])
        self.assertIsNone(res["blocked"])

    def test_a_blocker_is_reported_with_its_signal(self):
        res = self._tap({"text": "Cargo exceeds capacity"})
        self.assertFalse(res["tapped"])
        self.assertEqual(res["blocked"]["text"], "Cargo exceeds capacity")

    def test_a_blocker_does_NOT_trigger_recovery(self):
        """Recovery crosses worlds. From inside a primitive that is never allowed, and it is
        one half of the cycle this refactor exists to break."""
        import brain.recovery as recovery
        with mock.patch.object(recovery, "recover_to_port_overworld") as rec:
            self._tap({"text": "Cargo exceeds capacity"})
        rec.assert_not_called()

    def test_it_never_waits_for_the_sea(self):
        with mock.patch.object(sail_actions, "_is_loading_screen") as loading:
            self._tap("tapped")
        loading.assert_not_called()
