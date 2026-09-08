"""A material's Source panel lists places INSIDE the dialog. The map behind it does not.

Read live 2026-08-25, Matchlock Gun, whose panel says in full "Market: Barcelona, Seville":

    ['Barcelona', 'Chinook', 'Village', 'Seville', 'Ciguayo Village', 'Oriya Village',
     'Nubia', 'Village', 'Sioux Village', 'Bari Village', 'Lusitanian Villag', ...]

Every "village" there is a row of the WORLD MAP'S village list showing past the dialog's
left edge — Barcelona at cx 426 beside Chinook Village at cx 228, Seville at cx 404 beside
Ciguayo Village at cx 223. `Lusitanian Villag` is truncated because the dialog clips it;
`Nubia Village` split in two because it wrapped. No village sells Matchlock Gun and the
panel never said one did — the reader's fixed window (`cx < 700`) invented them, then
sorted by y so the two columns interleaved.

Live cost, 2026-09-08: `gather:Chinook:Matchlock Gun`. The fleet searched the port list for
'Chinook' twenty times and rightly refused; Seville, a real port two entries down, was
never reached.

Three source kinds exist — Market, Production and Village — and the panel names them in the
material's header ("Market, Production"). Production is a crafting feature and is dropped;
a village is a real source and is kept apart, because reaching one is a barter on a
different world-map tab, not a market visit.
"""

from __future__ import annotations

import os
import unittest

from PIL import Image

FRAMES = os.path.join(os.path.dirname(__file__), "..", "data", "test_frames", "source_panel")


class ThePanelIsReadAsTheGameShowsIt(unittest.TestCase):
    """Against the real frames, checked against the game's own data."""

    TRUTH = {
        "iron.png":          ["Amsterdam", "Barcelona", "Dublin", "Kuching"],
        "matchlock_gun.png": ["Barcelona", "Seville"],
        "candle.png":        ["Santa Island", "Tripoli"],
    }

    def _read(self, name):
        from vision.omniparser import parse_fast_cached
        from actions.village_remote_reader import read_material_sources_by_kind
        frame = Image.open(os.path.join(FRAMES, name))
        return read_material_sources_by_kind(frame, parse_fast_cached(frame))

    def test_the_market_list_is_exactly_what_the_panel_shows(self):
        for name, ports in self.TRUTH.items():
            with self.subTest(frame=name):
                self.assertEqual(ports, self._read(name).get("market"))

    def test_no_village_is_invented_from_the_map_behind_it(self):
        for name in self.TRUTH:
            with self.subTest(frame=name):
                got = self._read(name).get("village") or []
                self.assertEqual([], got,
                                 "none of these panels has a Village section")

    def test_production_is_dropped(self):
        """Iron's panel carries `Smelting Handbook: Uncut Ore - Iron`. It is not a place."""
        flat = sum(self._read("iron.png").values(), [])
        for junk in ("Production", "Smelting Handbook: Uncut Ore"):
            self.assertNotIn(junk, flat)


class AllThreeKindsAtOnce(unittest.TestCase):
    """Damascus Steel, captured live 2026-09-08 — the only panel we have with all three.

        Damascus Steel / Wares / Market, Village, Production
          Market       Beirut
          Village      Turk Village
          Production   Casting Handbook: Blade - Damascus Steel

    It also caught a second leak. Bounding x alone still read the world map ABOVE the
    dialog: 'Istanbul' (cy 32) and 'Thessaloniki' (cy 68) are port labels painted on the
    map behind, sitting in the same x band as the list and far above the title bar at
    y 167. They came back as Market sources for a good sold at one port.
    """

    def _read(self):
        from vision.omniparser import parse_fast_cached
        from actions.village_remote_reader import read_material_sources_by_kind
        frame = Image.open(os.path.join(FRAMES, "damascus_steel_all_three.png"))
        return read_material_sources_by_kind(frame, parse_fast_cached(frame))

    def test_the_market_is_the_one_port_the_panel_names(self):
        self.assertEqual(["Beirut"], self._read().get("market"))

    def test_the_village_section_is_kept(self):
        """A village is a real source — this is the case the other frames could not show."""
        self.assertEqual(["Turk Village"], self._read().get("village"))

    def test_production_is_still_dropped(self):
        flat = sum(self._read().values(), [])
        for junk in ("Casting Handbook", "Casting Handbook: Blade", "Production"):
            self.assertNotIn(junk, flat)

    def test_the_map_above_the_dialog_is_not_a_source(self):
        for label in ("Istanbul", "Thessaloniki", "Naples", "Syracuse"):
            self.assertNotIn(label, sum(self._read().values(), []))


class OnlyTheLeftPanelIsRead(unittest.TestCase):
    """The right two-thirds of the Source modal is a world map. Read the cream card only.

    A fraction of the dialog's width is the same fixed-window thinking that caused the
    original defect, one level in — so the panel's edge is MEASURED. Across the four
    captured panels the cream columns read 226-238 and the map beside them 117-163.
    """

    def test_the_panel_edge_is_measured_on_every_captured_panel(self):
        from actions.village_remote_reader import source_card_box, _list_panel
        for name in ("iron.png", "matchlock_gun.png", "candle.png",
                     "damascus_steel_all_three.png"):
            with self.subTest(frame=name):
                frame = Image.open(os.path.join(FRAMES, name))
                panel = _list_panel(frame, source_card_box(frame))
                self.assertIsNotNone(panel, "the cream card must be found, not assumed")
                x1, _y1, x2, _y2 = panel
                self.assertLess(x2 - x1, 700, "the map must fall outside the panel")

    def test_a_flat_dialog_yields_no_panel_rather_than_a_guess(self):
        from actions.village_remote_reader import _list_panel
        flat = Image.new("RGB", (2400, 1080), (200, 200, 200))
        self.assertIsNone(_list_panel(flat, (326, 167, 2074, 915)),
                          "no two-tone split means no edge to find")


class TheDialogIsFoundByItsOwnBox(unittest.TestCase):

    def test_the_source_card_is_measured_not_assumed(self):
        from actions.village_remote_reader import source_card_box
        frame = Image.open(os.path.join(FRAMES, "matchlock_gun.png"))
        box = source_card_box(frame)
        self.assertIsNotNone(box)
        x1, _y1, x2, _y2 = box
        self.assertLess(300, x1, "the map list at cx 188-278 must fall outside")
        self.assertGreater(x2, x1)

    def test_no_box_reads_nothing_rather_than_guessing(self):
        from actions.village_remote_reader import read_material_sources_by_kind
        self.assertEqual({}, read_material_sources_by_kind(Image.new("RGB", (2400, 1080)), []))


class AVillageIsASourceNotARejection(unittest.TestCase):

    def test_the_catalogue_tells_the_two_apart(self):
        from actions.village_remote_reader import resolve_source_port, resolve_source_village
        self.assertEqual("Barcelona", resolve_source_port("Barcelona"))
        self.assertIsNone(resolve_source_village("Barcelona"))
        self.assertEqual("Chinook Village", resolve_source_village("Chinook Village"))
        self.assertIsNone(resolve_source_port("Chinook Village"))

    def test_a_recipe_input_can_carry_both(self):
        """Some materials are sold at both, some at only one (user, 2026-09-08)."""
        from memory.barter_kb import RecipeInput
        i = RecipeInput(material="Diamond", ratio=1,
                        source_ports=["Barcelona"], source_villages=["Chinook Village"])
        self.assertEqual(["Barcelona"], i.to_dict()["source_ports"])
        self.assertEqual(["Chinook Village"], i.to_dict()["source_villages"])


class TheRerouteRefusesANonPort(unittest.TestCase):

    def test_a_stale_entry_cannot_become_a_course(self):
        import unittest.mock as mock
        from brain.mission_runner import MissionRunner
        r = MissionRunner.__new__(MissionRunner)
        r.good = "Birch Tree"
        recipe = mock.Mock(inputs=[mock.Mock(material="Matchlock Gun",
                                             source_ports=["Chinook", "Seville"])])
        with mock.patch("memory.barter_kb.load_recipe", lambda g: recipe), \
             mock.patch("memory.market_kb.season_of", lambda *a: None):
            self.assertEqual("Seville", r._another_source("Matchlock Gun", set()),
                             "'Chinook' is not a port — Seville is, and was skipped live")


if __name__ == "__main__":
    unittest.main()
