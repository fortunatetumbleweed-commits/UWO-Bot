"""A remote village check must persist what it learned, as the on-site check does.

Live 2026-09-04, the first mission for a good the KB had never seen. Berber Village read
cleanly from the world map:

    VillageTrade(good='Argan Oil', obtain=651,
                 materials={'Myrrh': 73, 'Mutton': 146, 'Almond': 146})

the plan was built straight from it — 7 rounds, buy 588 Myrrh + 1,176 Mutton + 1,176 Almond,
expect ~4,557 Argan Oil — and the run then died one step later:

    FAILED at step plan: no recipe for 'Argan Oil' even after the check wrote back

with recipes.json still holding the same thirteen goods it started the day with. The check
had not written back at all: `village_check.write_back_invariants` is called when the fleet
reads a village it is STANDING IN, and the world-map path never called it.

Every mission for an unknown good would have failed identically, and every mission for a
known one hid the bug — the KB already had what the write-back would have added, so
`load_recipe` succeeded on knowledge put there by some earlier on-site visit.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import tempfile
import unittest
from types import SimpleNamespace

import memory.barter_kb as kb
from brain.activities.world_map import WorldMapActivity

BERBER_BASE = {"amity_grade": "Neutral", "amity_points": None,
               "barters_used": 0, "barters_total": 5}
ARGAN = SimpleNamespace(good="Argan Oil", obtain=651,
                        materials={"Myrrh": 73, "Mutton": 146, "Almond": 146})
MOMORDICA = SimpleNamespace(good="Momordica Balsamina", obtain=651,
                            materials={"Tobacco": 146, "Peanut": 73, "Chicle": 146})


class _KB(unittest.TestCase):
    """Each test writes into a throwaway KB — never the repo's own."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self._saved = (kb._BARTER_DIR, kb._RECIPES_PATH, kb._VILLAGES_PATH)
        kb._BARTER_DIR = self.tmp
        kb._RECIPES_PATH = self.tmp / "recipes.json"
        kb._VILLAGES_PATH = self.tmp / "villages.json"

    def tearDown(self):
        kb._BARTER_DIR, kb._RECIPES_PATH, kb._VILLAGES_PATH = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _check(self, trades, base=None):
        a = WorldMapActivity.__new__(WorldMapActivity)
        a._base = dict(base if base is not None else BERBER_BASE)
        a._write_back(SimpleNamespace(village="Berber Village", good="Argan Oil"), trades)

    def _recipes(self):
        if not kb._RECIPES_PATH.exists():
            return []
        return sorted(json.loads(kb._RECIPES_PATH.read_text()))


class TheReadingSurvivesTheCheck(_KB):

    def test_the_good_the_mission_asked_for_is_loadable_afterwards(self):
        """`_run_mission_for` does exactly this, and got None."""
        self._check([ARGAN, MOMORDICA])
        r = kb.load_recipe("Argan Oil")
        self.assertIsNotNone(r, "the mission's very next step reloads this and failed")

    def test_the_materials_and_ratios_are_kept(self):
        self._check([ARGAN])
        r = kb.load_recipe("Argan Oil")
        self.assertEqual({i.material: i.ratio for i in r.inputs},
                         {"Myrrh": 73, "Mutton": 146, "Almond": 146})

    def test_the_village_is_recorded_against_the_good(self):
        self._check([ARGAN])
        self.assertIn("Berber Village", kb.load_recipe("Argan Oil").villages)

    def test_the_yield_is_stored_under_ITS_AMITY_GRADE(self):
        """651 is a Neutral yield. Filing it without the grade would make it look like the
        rate at any amity, and the rate rises with every grade."""
        self._check([ARGAN])
        self.assertEqual(kb.load_recipe("Argan Oil").output_per_round, {"Neutral": 651})

    def test_every_trade_on_the_panel_is_kept_not_just_the_one_asked_for(self):
        """The read cost the same either way, and the next mission may want the other."""
        self._check([ARGAN, MOMORDICA])
        self.assertEqual(self._recipes(), ["argan_oil", "momordica_balsamina"])

    def test_the_village_record_carries_its_daily_allowance(self):
        self._check([ARGAN, MOMORDICA])
        v = json.loads(kb._VILLAGES_PATH.read_text())["berber_village"]
        self.assertEqual(v["amity"], "Neutral")
        self.assertEqual(v["barter_rounds_total"], 5)
        self.assertEqual(sorted(v["eligible_goods"]), ["Argan Oil", "Momordica Balsamina"])


class ItWritesNothingItShouldNot(_KB):

    def test_a_trade_with_no_materials_is_not_a_recipe(self):
        """A tooltip over a locked tile reads as a good with no inputs."""
        self._check([SimpleNamespace(good="he Village", obtain=0, materials={})])
        self.assertNotIn("he_village", self._recipes())

    def test_it_does_not_invent_source_ports(self):
        """Source ports are learned at MARKETS. A remote read cannot know them, and claiming
        one would send a gather leg to a port that may not stock the material."""
        self._check([ARGAN])
        for i in kb.load_recipe("Argan Oil").inputs:
            with self.subTest(i.material):
                self.assertEqual(list(i.source_ports or []), [])

    def test_a_write_back_failure_never_breaks_the_check(self):
        """This is bookkeeping about a reading that already succeeded."""
        a = WorldMapActivity.__new__(WorldMapActivity)
        a._base = None                       # forces the duck to fail
        a._write_back(SimpleNamespace(village="Berber Village", good="Argan Oil"),
                      [SimpleNamespace(good=None, obtain=None, materials=None)])


if __name__ == "__main__":
    unittest.main()
