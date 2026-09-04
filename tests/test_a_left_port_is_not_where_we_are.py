# tests/test_a_left_port_is_not_where_we_are.py
#
# PLACE DATA DIES ON LEAVING (CLAUDE.md; user, 2026-09-02).
#
# `_refined_state` remembers the last port the SCREEN named, because a building interior has
# no banner — nothing on a market's screen says "Lisboa" — and a market activity that asked
# the state where it was reported port='' (2026-08-26). That is a good fix for that case.
#
# It was offered EVERYWHERE a name was missing, and its comment justified that with "it can
# only ever be stale in the one direction that matters least — the bot walks into a building
# and out again at the same port". Going to sea is leaving, and the fleet arrives elsewhere.
#
# Live 2026-09-02: four seconds after the sea, Faro's UI had not finished drawing, so
# `read_port_name` correctly returned None — and this handed the voyage 'Lisboa', last named
# 2.5 minutes and one departure earlier. The arrival check compared it against Faro and
# failed the mission with the fleet standing in Faro.
#
#     15:56:25  → port_overworld (family-classifier conf=1.00, port=None)
#     15:56:27  the voyage ended at 'Lisboa', not 'Faro' — the leg is not done
#
# Two rules, kept apart on purpose: a building may borrow the name; the sea DESTROYS it.

import types
import unittest
from unittest import mock

import brain.run_goal as rg


def _looking_at(where, port=None, detail=""):
    """Drive _refined_state with one perceive result."""
    res = types.SimpleNamespace(
        to_location_dict=lambda: {"location": where, "port": port, "detail": detail},
        sub_menu=None, scene_type=None, frame=None)
    return mock.patch("brain.perceive.perceive", return_value=res)


class ABuildingMayBorrowTheName(unittest.TestCase):
    """The case the memory exists for — do not regress it."""

    def setUp(self):
        rg._last_named_port = None

    def test_a_market_interior_reports_the_port_it_is_in(self):
        with _looking_at("port_overworld", port="Lisboa"):
            rg._refined_state()
        with _looking_at("building", detail="building: market"):
            self.assertEqual(rg._refined_state().port, "Lisboa")

    def test_a_sub_menu_does_too(self):
        with _looking_at("port_overworld", port="Lisboa"):
            rg._refined_state()
        with _looking_at("sub_menu", detail="sub_menu: purchase"):
            self.assertEqual(rg._refined_state().port, "Lisboa")


class TheSeaDestroysIt(unittest.TestCase):
    def setUp(self):
        rg._last_named_port = None

    def test_going_to_sea_forgets_the_port(self):
        with _looking_at("port_overworld", port="Lisboa"):
            rg._refined_state()
        with _looking_at("sea"):
            self.assertIsNone(rg._refined_state().port)
        self.assertIsNone(rg._last_named_port, "cleared, not merely unused")

    def test_the_next_unnamed_port_is_not_given_the_old_one(self):
        """The live failure, end to end: Lisboa → sea → an undrawn Faro."""
        with _looking_at("port_overworld", port="Lisboa"):
            rg._refined_state()
        with _looking_at("sea"):
            rg._refined_state()
        with _looking_at("port_overworld", port=None):          # frame 33: still drawing
            arrived = rg._refined_state().port
        self.assertIsNone(
            arrived, "an undrawn port must report no name, never the port we sailed from")


class AnUndrawnPortSaysSoRatherThanGuessing(unittest.TestCase):
    def setUp(self):
        rg._last_named_port = None

    def test_a_port_with_no_readable_name_reports_none(self):
        """Even without a sea tick in between. A port_overworld HAS a banner, so a missing
        name is a fact about the frame — it has not finished drawing — and that is the useful
        answer. Substituting is what turned a render delay into a wrong-port verdict."""
        with _looking_at("port_overworld", port="Lisboa"):
            rg._refined_state()
        with _looking_at("port_overworld", port=None):
            self.assertIsNone(rg._refined_state().port)

    def test_a_named_port_still_replaces_the_memory_immediately(self):
        with _looking_at("port_overworld", port="Lisboa"):
            rg._refined_state()
        with _looking_at("port_overworld", port="Faro"):
            self.assertEqual(rg._refined_state().port, "Faro")
        self.assertEqual(rg._last_named_port, "Faro")

    def test_a_village_does_not_borrow_a_port_name(self):
        """A village is a settlement in its own right; handing it the last port would name it
        something it is not."""
        with _looking_at("port_overworld", port="Lisboa"):
            rg._refined_state()
        with _looking_at("village", port=None):
            self.assertIsNone(rg._refined_state().port)
