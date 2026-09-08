"""`Textiles` is a material AND a category word, and the pill filter matched on the word.

Live 2026-09-07, the first Box of Nutmeg run. The Village Info trade list at Melanesian
Village showed every row plainly (frame 13 of trace_barter_cmd_2026-09-07T22-37-17):

    Box of Nutmeg  616  Spices
    Ebony          101  Wares
    Coral          175  Jewelry
    Textiles       175  Fabrics        <- legible, on the very first look

and the reader said:

    [village_check] 'Box of Nutmeg' not complete yet — still missing ['textiles']; scrolling on
    ... eight scrolls ...
    the trade list at Melanesian Village did not complete in 8 scrolls — PARTIAL
    status failed

`_row_names` excluded the category pill beside each row by matching its TEXT against
`_CATEGORY_WORDS`, which contains 'textile' and 'textiles'. So the material was discarded as
if it were the pill: four badges, three names, and the fourth row came out `name='' qty=0`.
The reader then scrolled past the row it already had, and the third material slot was filled
with a `Bow 73` from far down the list.

The pill is distinguishable by POSITION. On that frame names start at x1 1827/1829/1831/1833
and pills at 1869/1871/1872/1872 — four rows out of four, a 36px gap with 6px of jitter
inside each column. Same idiom the row parser already uses to tell a material from a good.

Never bit before because no earlier recipe took Textiles. `Livestock`, `Firearms`,
`Medicine`, `Perfume` and `Ore` are all in that word list too, and all plausible materials.
"""
import os
import unittest


class TheTradeListReadsInOneLook(unittest.TestCase):
    STAGE = "tests/stage_suite/frames/melanesian_textiles_is_a_material.png"

    def _elements(self):
        if not os.path.exists(self.STAGE):
            self.skipTest("stage frame not available")
        from PIL import Image
        from actions.village_check import trade_list_elements
        return trade_list_elements(Image.open(self.STAGE))

    def test_the_recipe_is_complete_without_scrolling(self):
        from actions.village_check import parse_trade_list
        trades = {t.good: t for t in parse_trade_list(self._elements())}
        self.assertIn("Box of Nutmeg", trades)
        self.assertEqual(trades["Box of Nutmeg"].materials,
                         {"Ebony": 101, "Coral": 175, "Textiles": 175})

    def test_textiles_survives_the_pill_filter(self):
        from actions.village_check import _qty_elements, _row_names
        els = self._elements()
        names = {(e.label or "").strip() for e in _row_names(els, _qty_elements(els))}
        self.assertIn("Textiles", names, "the material was dropped as if it were a category")

    def test_and_the_pills_themselves_are_still_excluded(self):
        from actions.village_check import _qty_elements, _row_names
        els = self._elements()
        names = {(e.label or "").strip() for e in _row_names(els, _qty_elements(els))}
        for pill in ("Fabrics", "Wares", "Jewelry", "Spices"):
            with self.subTest(pill=pill):
                self.assertNotIn(pill, names)


class TheColumnsAreActuallySeparated(unittest.TestCase):
    """The measurement the rule rests on, asserted rather than trusted."""
    STAGE = "tests/stage_suite/frames/melanesian_textiles_is_a_material.png"

    def test_the_pill_column_is_indented_past_the_name_column(self):
        if not os.path.exists(self.STAGE):
            self.skipTest("stage frame not available")
        from PIL import Image
        from actions.village_check import _PILL_INDENT_PX, _labelled, trade_list_elements
        els = trade_list_elements(Image.open(self.STAGE))
        by = {(e.label or "").strip(): e.x1 for e in _labelled(els)}
        names = [by[n] for n in ("Ebony", "Coral", "Textiles", "Sago") if n in by]
        pills = [by[p] for p in ("Wares", "Jewelry", "Fabrics", "Spices") if p in by]
        self.assertTrue(names and pills, "the frame should show both columns")
        self.assertGreater(min(pills) - min(names), _PILL_INDENT_PX,
                           "the gap the rule depends on")
        self.assertLess(max(names) - min(names), _PILL_INDENT_PX,
                        "and the name column must sit inside the tolerance")


if __name__ == "__main__":
    unittest.main()
