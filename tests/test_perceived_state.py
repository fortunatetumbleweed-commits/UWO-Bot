"""A2 Phase 0 — PerceivedState scaffold.

The load-bearing invariant is round-trip fidelity: deriving a structured view
from a legacy `location` and mapping it back must reproduce the original, for
every state the classifier can emit. This lets authority flip to the structured
type later without breaking the 40+ consumers of PerceiveResult.state.
"""
import unittest

from brain.perceived_state import (
    PerceivedState, BASE_OVERWORLD, BASE_PANEL, BASE_WORLD_MAP, BASE_LOADING,
    BASE_UNKNOWN, MODE_SEA, MODE_PORT, OVERLAY_NONE, OVERLAY_DIALOG,
    OVERLAY_MAIN_MENU,
)

# Every legacy `location` string _classify_nav_state can return, plus a learned
# fingerprint id (dynamic states via sc.state).
LEGACY_STATES = [
    "sea", "port_overworld", "world_map", "loading", "pending",
    "village", "building", "main_menu", "unknown", "learned_quick_revive",
]


class RoundTripTests(unittest.TestCase):
    def test_from_legacy_then_legacy_state_is_identity(self):
        for loc in LEGACY_STATES:
            with self.subTest(loc=loc):
                got = PerceivedState.from_legacy(loc, port="Cairo").legacy_state()
                self.assertEqual(got, loc)

    def test_overlay_does_not_break_round_trip(self):
        # An interruptor still up must not change the derived legacy state
        # (overlay is orthogonal to base).
        for loc in LEGACY_STATES:
            with self.subTest(loc=loc):
                ps = PerceivedState.from_legacy(loc, has_overlay=True)
                # main_menu is itself an overlay; the others keep their base state
                self.assertEqual(ps.legacy_state(), loc)


class FieldMappingTests(unittest.TestCase):
    def test_sea_is_overworld_sea(self):
        ps = PerceivedState.from_legacy("sea")
        self.assertEqual(ps.base, BASE_OVERWORLD)
        self.assertEqual(ps.mode, MODE_SEA)

    def test_port_is_overworld_port(self):
        ps = PerceivedState.from_legacy("port_overworld", port="Cairo")
        self.assertEqual(ps.base, BASE_OVERWORLD)
        self.assertEqual(ps.mode, MODE_PORT)
        self.assertEqual(ps.identity, "Cairo")

    def test_village_is_panel_context_village(self):
        ps = PerceivedState.from_legacy("village", port="Berber Village")
        self.assertEqual(ps.base, BASE_PANEL)
        self.assertEqual(ps.context, "Village")

    def test_world_map_and_loading(self):
        self.assertEqual(PerceivedState.from_legacy("world_map").base, BASE_WORLD_MAP)
        self.assertEqual(PerceivedState.from_legacy("loading").base, BASE_LOADING)
        self.assertEqual(PerceivedState.from_legacy("pending").base, BASE_LOADING)

    def test_main_menu_is_overlay(self):
        ps = PerceivedState.from_legacy("main_menu")
        self.assertEqual(ps.overlay, OVERLAY_MAIN_MENU)

    def test_has_overlay_sets_dialog(self):
        self.assertEqual(
            PerceivedState.from_legacy("sea", has_overlay=True).overlay,
            OVERLAY_DIALOG,
        )
        self.assertEqual(
            PerceivedState.from_legacy("sea").overlay, OVERLAY_NONE,
        )

    def test_unknown(self):
        self.assertEqual(PerceivedState.from_legacy("unknown").base, BASE_UNKNOWN)
        self.assertEqual(PerceivedState.from_legacy("").base, BASE_UNKNOWN)


if __name__ == "__main__":
    unittest.main()
