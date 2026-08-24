"""Tests for the remote village barter-check parsers — element fixtures mirror the LIVE
Apache Village read (2026-08-20): goods flush-left without a pin, materials indented with
a location pin, materials continuing across a scroll boundary."""
import types
import unittest
from unittest import mock

from actions.village_check import (parse_trade_list, merge_trade_screens, parse_base_tab,
                                   material_pins, screen_signature, write_back_invariants,
                                   VillageCheck, VillageTrade, _materials_needing_sources,
                                   _missing_known_materials, _reconcile_with_known,
                                   _target_complete)


def _el(etype, label, x1, y1, w=120, h=100):
    return types.SimpleNamespace(element_type=etype, label=label,
                                 x1=x1, y1=y1, x2=x1 + w, y2=y1 + h,
                                 cx=x1 + w // 2, cy=y1 + h // 2)


def _row(name, qty, y, indented, pin):
    els = [_el("button", qty, 1840 if indented else 1810, y),
           _el("text", name, 1950, y + 10, w=110, h=32)]
    if pin:
        els.append(_el("icon", "icon", 2280, y + 60, w=46, h=48))
    return els


class ParseTradeListTests(unittest.TestCase):
    def test_goods_and_materials_by_indent(self):
        els = (_row("Camas", "953", 400, indented=False, pin=False)
               + _row("Avocado", "130", 540, indented=True, pin=True)
               + _row("Cassava", "150", 650, indented=True, pin=True)
               + _row("Pulque", "1,022", 770, indented=False, pin=False))
        trades = parse_trade_list(els)
        self.assertEqual([t.good for t in trades], ["Camas", "Pulque"])
        self.assertEqual(trades[0].obtain, 953)
        self.assertEqual(trades[0].materials, {"Avocado": 130, "Cassava": 150})
        self.assertEqual(trades[1].obtain, 1022)

    def test_continuation_screen_starts_with_materials(self):
        # After a scroll the first rows can be the PREVIOUS good's remaining materials.
        s1 = parse_trade_list(_row("Pulque", "1,022", 400, False, False)
                              + _row("Coral", "150", 540, True, True))
        s2 = parse_trade_list(_row("Silver", "150", 400, True, True)
                              + _row("Wampum", "635", 520, False, False)
                              + _row("Platinum", "150", 660, True, True))
        merged = merge_trade_screens([s1, s2])
        self.assertEqual([t.good for t in merged], ["Pulque", "Wampum"])
        self.assertEqual(merged[0].materials, {"Coral": 150, "Silver": 150})
        self.assertEqual(merged[1].materials, {"Platinum": 150})

    def test_merge_dedupes_overlapping_screens(self):
        s = parse_trade_list(_row("Camas", "953", 400, False, False)
                             + _row("Avocado", "130", 540, True, True))
        merged = merge_trade_screens([s, s])          # same screen twice (scroll didn't move)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].materials, {"Avocado": 130})


class ParseBaseTabTests(unittest.TestCase):
    def test_reads_amity_and_daily_progress(self):
        els = [_el("text", "Friendly", 2050, 330, w=90, h=30),
               _el("button", "93,958/100,000", 1950, 370, w=260, h=42),
               _el("text", "Daily Barter Progress", 1850, 440, w=230, h=30),
               _el("text", "0/7", 2290, 440, w=50, h=28)]
        out = parse_base_tab(els)
        self.assertEqual(out["amity_grade"], "Friendly")
        self.assertEqual(out["amity_points"], (93958, 100000))
        self.assertEqual((out["barters_used"], out["barters_total"]), (0, 7))


class MaterialPinTests(unittest.TestCase):
    def test_pins_map_to_their_row_name(self):
        els = (_row("Camas", "953", 400, indented=False, pin=False)
               + _row("Avocado", "130", 540, indented=True, pin=True)
               + _row("Cassava", "150", 650, indented=True, pin=True))
        pins = material_pins(els)
        self.assertEqual(sorted(pins), ["Avocado", "Cassava"])
        self.assertEqual(pins["Avocado"][0], 2303)          # pin column centre
        self.assertNotIn("Camas", pins)                     # goods have no pin

    def test_signature_detects_a_scroll_that_did_not_move(self):
        s = parse_trade_list(_row("Camas", "953", 400, False, False)
                             + _row("Avocado", "130", 540, True, True))
        self.assertEqual(screen_signature(s), screen_signature(s))
        other = parse_trade_list(_row("Pulque", "1,022", 400, False, False))
        self.assertNotEqual(screen_signature(s), screen_signature(other))


class VillageCheckTests(unittest.TestCase):
    def _check(self):
        return VillageCheck(village="Apache Village", ok=True, amity_grade="Friendly",
                            amity_points=(93958, 100000), barters_used=2, barters_total=7,
                            trades=[VillageTrade("Camas", 953, {"Avocado": 130})],
                            sources={"Avocado": ["Tenochtitlan"]})

    def test_rounds_remaining_and_lookup(self):
        c = self._check()
        self.assertEqual(c.rounds_remaining, 5)
        self.assertEqual(c.trade_for("camas").obtain, 953)   # case-insensitive
        self.assertIsNone(c.trade_for("Pulque"))

    def test_rounds_remaining_is_none_when_unread(self):
        self.assertIsNone(VillageCheck(village="X").rounds_remaining)

    def test_write_back_keeps_invariants_and_records_last_seen_quantities(self):
        saved = {}
        with mock.patch("memory.barter_kb.load_recipe", return_value=None), \
             mock.patch("memory.barter_kb.save_recipe",
                        side_effect=lambda r: saved.setdefault("recipe", r)), \
             mock.patch("memory.barter_kb.save_village",
                        side_effect=lambda v: saved.setdefault("village", v)):
            write_back_invariants(self._check())
        recipe = saved["recipe"]
        self.assertEqual([i.material for i in recipe.inputs], ["Avocado"])
        self.assertEqual(recipe.inputs[0].source_ports, ["Tenochtitlan"])
        self.assertEqual(recipe.inputs[0].ratio, 130)            # last-seen volatile
        self.assertEqual(recipe.output_per_round, {"Friendly": 953})
        self.assertEqual(recipe.villages, ["Apache Village"])
        self.assertEqual(saved["village"].barter_rounds_remaining, 5)

    def test_write_back_preserves_known_sources_when_the_pin_was_not_read(self):
        from memory.barter_kb import BarterRecipe, RecipeInput
        existing = BarterRecipe(good="Camas", villages=["Apache Village"],
                                inputs=[RecipeInput("Avocado", 999, ["Havana"])])
        check = self._check()
        check.sources = {}                                   # no pin read this time
        saved = {}
        with mock.patch("memory.barter_kb.load_recipe", return_value=existing), \
             mock.patch("memory.barter_kb.save_recipe",
                        side_effect=lambda r: saved.setdefault("recipe", r)), \
             mock.patch("memory.barter_kb.save_village"):
            write_back_invariants(check)
        self.assertEqual(saved["recipe"].inputs[0].source_ports, ["Havana"])
        self.assertEqual(saved["recipe"].villages, ["Apache Village"])   # not duplicated


class SourceLearningTargetTests(unittest.TestCase):
    """Which materials the check should spend taps on (pins are tap-costly)."""

    def _screen(self, *rows):
        els = []
        for name, qty, y, indented in rows:
            els += _row(name, qty, y, indented, pin=indented)
        return parse_trade_list(els)

    def test_only_unknown_materials_of_the_named_good(self):
        s1 = self._screen(("Camas", "953", 400, False), ("Avocado", "130", 540, True))
        s2 = self._screen(("Cassava", "150", 400, True), ("Pulque", "1,022", 520, False),
                          ("Coral", "150", 660, True))
        # screen 1: Camas' own material, source unknown → wanted
        self.assertEqual(list(_materials_needing_sources([s1], s1, "Camas", {}, {})),
                         ["Avocado"])
        # screen 2 continues Camas (Cassava) then starts Pulque → only Cassava wanted
        self.assertEqual(list(_materials_needing_sources([s1, s2], s2, "Camas", {}, {})),
                         ["Cassava"])

    def test_known_and_already_learned_materials_are_skipped(self):
        s = self._screen(("Camas", "953", 400, False), ("Avocado", "130", 540, True),
                         ("Cassava", "150", 650, True))
        known = {"avocado": ["Havana"]}
        wanted = _materials_needing_sources([s], s, "Camas", known, {"Cassava": ["X"]})
        self.assertEqual(wanted, {})

    def test_no_good_filter_wants_every_unknown_material(self):
        s = self._screen(("Camas", "953", 400, False), ("Avocado", "130", 540, True),
                         ("Pulque", "1,022", 650, False), ("Coral", "150", 760, True))
        self.assertEqual(sorted(_materials_needing_sources([s], s, None, {}, {})),
                         ["Avocado", "Coral"])


class TooltipRegressionTests(unittest.TestCase):
    """The amity-lock tooltip must not steal a row's name (live 2026-08-21).

    The tooltip sat 406px to the RIGHT of the Cassava row but aligned 4px BETTER
    vertically, so a rule that sorted on vertical alignment first picked it — and
    'he Village' was written into recipes.json as one of Camas's materials."""

    def _cassava_row_with_tooltip(self):
        return [
            _real("button", "559", 1692, 396, 1830, 529),
            _real("text", "Camas", 1834, 408, 1934, 440),
            _real("button", "190", 1722, 644, 1828, 753),
            _real("text", "Cassava", 1829, 659, 1953, 691),     # 1px right,  Δy 15
            _real("icon", "icon", 2161, 701, 2208, 748),
            _real("text", "he Village", 2234, 633, 2396, 687),  # 406px right, Δy 11
        ]

    def test_the_adjacent_label_wins_not_the_better_aligned_overlay(self):
        trades = parse_trade_list(self._cassava_row_with_tooltip())
        self.assertEqual(trades[0].materials, {"Cassava": 190})
        self.assertNotIn("he Village", trades[0].materials)

    def test_a_far_away_text_cannot_claim_a_row_at_all(self):
        els = [_real("button", "190", 1722, 644, 1828, 753),
               _real("text", "he Village", 2234, 633, 2396, 687)]
        self.assertEqual(parse_trade_list(els), [])   # no adjacent label → no row


if __name__ == "__main__":
    unittest.main()


# ── Regression: the REAL panel, captured live 2026-08-21 ─────────────────────
# The 2026-08-20 constants (tiles 1780-1900, names 1940-2120, pins 2270-2340) rejected
# every row of this capture and the trade list read back EMPTY on the first live run.
# The same panel had slid ~110px left — the camera-cutout safe area the game re-bakes at
# each world-switch (AUDIT.md). These fixtures are the actual OmniParser output; they are
# what keeps the parser honest about being layout-relative rather than position-tuned.

def _real(type_, label, x1, y1, x2, y2):
    return types.SimpleNamespace(element_type=type_, label=label, x1=x1, y1=y1, x2=x2,
                                 y2=y2, cx=(x1 + x2) // 2, cy=(y1 + y2) // 2)


def _apache_2026_08_21_screen0():
    """Apache Village, Barter tab, first screen — Camas and Pulque."""
    return [
        _real("button", "559", 1692, 396, 1830, 529),
        _real("text", "Camas", 1834, 408, 1934, 440),
        _real("button", "150", 1722, 536, 1829, 640),
        _real("text", "Avocado", 1831, 549, 1955, 581),
        _real("icon", "icon", 2162, 591, 2209, 638),
        _real("button", "190", 1722, 644, 1828, 753),
        _real("text", "Cassava", 1829, 659, 1953, 691),
        _real("icon", "icon", 2161, 701, 2208, 748),
        _real("button", "663", 1695, 768, 1830, 900),
        _real("text", "Pulque", 1833, 777, 1933, 809),
        # Junk OCR at the bottom of the real capture — must not become a row.
        _real("text", "4.0803.091.322 2608211115 At", 1952, 1055, 2360, 1080),
    ]


class LivePanelRegressionTests(unittest.TestCase):
    def test_the_real_panel_parses(self):
        trades = parse_trade_list(_apache_2026_08_21_screen0())
        self.assertEqual([t.good for t in trades], ["Camas", "Pulque"])
        self.assertEqual(trades[0].obtain, 559)
        self.assertEqual(trades[0].materials, {"Avocado": 150, "Cassava": 190})

    def test_the_pin_not_the_indent_decides_material_vs_good(self):
        # Pulque's tile is only 3px off the flush-left base — an indent threshold alone is
        # a coin flip at that margin; the pin is unambiguous.
        els = _apache_2026_08_21_screen0()
        self.assertEqual(sorted(material_pins(els)), ["Avocado", "Cassava"])

    def test_a_continuation_screen_of_pure_materials_is_not_read_as_goods(self):
        # Every row here is a material, so the indent BASELINE is itself a material and
        # indent-classification would call them all goods. The pin keeps it right.
        els = [
            _real("button", "170", 1724, 396, 1830, 500),
            _real("text", "Coral", 1834, 408, 1934, 440),
            _real("icon", "icon", 2162, 431, 2209, 478),
            _real("button", "170", 1723, 512, 1830, 616),
            _real("text", "Silver", 1834, 524, 1934, 556),
            _real("icon", "icon", 2162, 547, 2209, 594),
        ]
        trades = parse_trade_list(els)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].good, "")          # continuation, merged by the caller
        self.assertEqual(trades[0].materials, {"Coral": 170, "Silver": 170})

    def test_the_panel_may_sit_anywhere_horizontally(self):
        # Same layout shifted 300px right (a different cutout bake) must parse identically.
        shifted = [_real(e.element_type, e.label, e.x1 + 300, e.y1, e.x2 + 300, e.y2)
                   for e in _apache_2026_08_21_screen0()]
        trades = parse_trade_list(shifted)
        self.assertEqual([t.good for t in trades], ["Camas", "Pulque"])
        self.assertEqual(trades[0].materials, {"Avocado": 150, "Cassava": 190})

    def test_a_nameless_good_is_not_written_to_the_kb(self):
        # The amity-lock tooltip read as a good called 'he Village' with no materials.
        saved = []
        check = VillageCheck(village="Apache Village", ok=True, amity_grade="Friendly",
                             trades=[VillageTrade("he Village", 407, {}),
                                     VillageTrade("Camas", 559, {"Avocado": 150})])
        with mock.patch("memory.barter_kb.load_recipe", return_value=None), \
             mock.patch("memory.barter_kb.save_recipe", side_effect=saved.append), \
             mock.patch("memory.barter_kb.save_village"):
            write_back_invariants(check)
        self.assertEqual([r.good for r in saved], ["Camas"])

class PerVillageMaterialsTests(unittest.TestCase):
    """The SAME good takes different materials at different villages — Box of Nutmeg is
    Ebony+Coral+Textiles at Melanesian and Khmer, but Ebony+Coral+Dhaka Muslin at Malay
    (user 2026-08-21). One shared `inputs` list cannot express that, and planning a gather
    from the union would buy a material the village will not accept."""

    def _recipe(self):
        from memory.barter_kb import BarterRecipe, RecipeInput
        r = BarterRecipe(good="Box of Nutmeg",
                         inputs=[RecipeInput("Ebony", 136, ["Jakarta"]),
                                 RecipeInput("Coral", 180, ["Male"]),
                                 RecipeInput("Textiles", 180, ["Kolkata"]),
                                 RecipeInput("Dhaka Muslin", 180, ["Pegu"])])
        r.village_inputs = {
            "melanesian_village": [RecipeInput("Ebony", 136, ["Jakarta"]),
                                   RecipeInput("Coral", 180, ["Male"]),
                                   RecipeInput("Textiles", 180, ["Kolkata"])],
            "malay_village": [RecipeInput("Ebony", 136, ["Jakarta"]),
                              RecipeInput("Coral", 180, ["Male"]),
                              RecipeInput("Dhaka Muslin", 180, ["Pegu"])]}
        return r

    def test_each_village_gets_its_own_material_list(self):
        r = self._recipe()
        self.assertEqual([i.material for i in r.inputs_for("Melanesian Village")],
                         ["Ebony", "Coral", "Textiles"])
        self.assertEqual([i.material for i in r.inputs_for("Malay Village")],
                         ["Ebony", "Coral", "Dhaka Muslin"])

    def test_an_unknown_village_falls_back_to_the_union(self):
        self.assertEqual(len(self._recipe().inputs_for("Khmer Village")), 4)
        self.assertEqual(len(self._recipe().inputs_for(None)), 4)

    def test_it_survives_a_kb_round_trip(self):
        from memory.barter_kb import BarterRecipe
        back = BarterRecipe.from_dict(self._recipe().to_dict())
        self.assertEqual([i.material for i in back.inputs_for("Malay Village")],
                         ["Ebony", "Coral", "Dhaka Muslin"])
        self.assertEqual(back.village_inputs["malay_village"][2].source_ports, ["Pegu"])

    def test_the_completeness_guard_is_village_scoped(self):
        # Textiles is absent at Malay by design — that must NOT read as a partial read.
        trades = [VillageTrade("Box of Nutmeg", 737,
                               {"Ebony": 136, "Coral": 180, "Dhaka Muslin": 180})]
        with mock.patch("memory.barter_kb.load_recipe", return_value=self._recipe()):
            self.assertEqual(
                _missing_known_materials(trades, "Box of Nutmeg", "Malay Village"), [])
            # …but a genuinely missing one at Melanesian still trips it.
            self.assertEqual(
                _missing_known_materials(
                    [VillageTrade("Box of Nutmeg", 737, {"Ebony": 136, "Textiles": 180})],
                    "Box of Nutmeg", "Melanesian Village"), ["Coral"])

    def test_gather_sources_come_from_the_village_not_the_union(self):
        from brain.barter_mission_live import material_sources_from_recipe
        src = material_sources_from_recipe(self._recipe(), "Melanesian Village")
        self.assertNotIn("Dhaka Muslin", src)      # never shop for what Malay wants
        self.assertEqual(sorted(src), ["Coral", "Ebony", "Textiles"])


class InvariantTypesVolatileNumbersTests(unittest.TestCase):
    """The user's model, enforced: material TYPES for a good at a village do not change;
    the NUMBERS re-roll every ~6h. So for a known good+village the KB's type set is
    authoritative and the live read supplies only quantities.

    Live 2026-08-21: an 8-screen scroll swept Glass Bead and Candle into Box of Nutmeg
    (their own good's name went undetected), and the plan set off to buy them in Tripoli
    and Amsterdam."""

    def _recipe(self):
        from memory.barter_kb import BarterRecipe, RecipeInput
        r = BarterRecipe(good="Box of Nutmeg",
                         inputs=[RecipeInput("Ebony", 136), RecipeInput("Coral", 180),
                                 RecipeInput("Textiles", 180)])
        r.village_inputs = {"melanesian_village": [RecipeInput("Ebony", 136),
                                                   RecipeInput("Coral", 180),
                                                   RecipeInput("Textiles", 180)]}
        return r

    def test_materials_from_another_good_are_dropped(self):
        trades = [VillageTrade("Box of Nutmeg", 737,
                               {"Ebony": 136, "Coral": 180, "Textiles": 180,
                                "Glass Bead": 65, "Candle": 75})]
        with mock.patch("memory.barter_kb.load_recipe", return_value=self._recipe()):
            _reconcile_with_known(trades, "Box of Nutmeg", "Melanesian Village")
        self.assertEqual(sorted(trades[0].materials), ["Coral", "Ebony", "Textiles"])

    def test_the_LIVE_quantities_are_kept_not_the_kb_ones(self):
        # Numbers re-roll; types don't. The re-rolled values must survive reconciliation.
        trades = [VillageTrade("Box of Nutmeg", 737,
                               {"Ebony": 152, "Coral": 204, "Textiles": 199,
                                "Candle": 75})]
        with mock.patch("memory.barter_kb.load_recipe", return_value=self._recipe()):
            _reconcile_with_known(trades, "Box of Nutmeg", "Melanesian Village")
        self.assertEqual(trades[0].materials,
                         {"Ebony": 152, "Coral": 204, "Textiles": 199})

    def test_a_new_good_is_learned_as_read(self):
        trades = [VillageTrade("Sofrito", 400, {"Chili": 50, "Garlic": 30})]
        with mock.patch("memory.barter_kb.load_recipe", return_value=None):
            _reconcile_with_known(trades, "Sofrito", "Somewhere New")
        self.assertEqual(trades[0].materials, {"Chili": 50, "Garlic": 30})

    def test_an_unnamed_good_row_ends_the_previous_goods_materials(self):
        # The structural half: a good row we cannot name must not let the NEXT good's
        # materials attach to the PREVIOUS good.
        els = [
            _real("button", "737", 1694, 397, 1830, 528),
            _real("text", "Box of Nutmeg", 1833, 403, 2060, 448),
            _real("button", "136", 1723, 536, 1829, 638),
            _real("text", "Ebony", 1827, 546, 1932, 589),
            _real("icon", "icon", 2162, 589, 2208, 638),
            _real("button", "199", 1696, 700, 1830, 830),      # a GOOD (no pin), unnamed
            _real("button", "65", 1725, 860, 1826, 918),       # its material
            _real("text", "Glass Bead", 1829, 866, 1980, 900),
            _real("icon", "icon", 2161, 870, 2209, 916),
        ]
        trades = parse_trade_list(els)
        nutmeg = next(t for t in trades if t.good == "Box of Nutmeg")
        self.assertEqual(nutmeg.materials, {"Ebony": 136})
        self.assertNotIn("Glass Bead", nutmeg.materials)


class StopScrollingWhenTheTargetIsReadTests(unittest.TestCase):
    """Checking a village for ONE good should stop as soon as that good is fully read
    (user 2026-08-21). It saves taps, and scrolling on is exactly how another good's rows
    got swept into this one."""

    def _recipe(self):
        from memory.barter_kb import BarterRecipe, RecipeInput
        r = BarterRecipe(good="Box of Nutmeg", inputs=[RecipeInput("Ebony", 136),
                                                       RecipeInput("Coral", 180),
                                                       RecipeInput("Textiles", 180)])
        r.village_inputs = {"melanesian_village": list(r.inputs)}
        return r

    def test_complete_once_every_known_material_has_a_quantity(self):
        trades = [VillageTrade("Box of Nutmeg", 737,
                               {"Ebony": 136, "Coral": 180, "Textiles": 180})]
        with mock.patch("memory.barter_kb.load_recipe", return_value=self._recipe()):
            self.assertTrue(_target_complete(trades, "Box of Nutmeg", "Melanesian Village"))

    def test_not_complete_while_a_material_is_still_missing(self):
        trades = [VillageTrade("Box of Nutmeg", 737, {"Ebony": 136, "Coral": 180})]
        with mock.patch("memory.barter_kb.load_recipe", return_value=self._recipe()):
            self.assertFalse(_target_complete(trades, "Box of Nutmeg", "Melanesian Village"))

    def test_an_unknown_good_is_complete_once_the_next_good_appears(self):
        # No KB list to check against, so the NEXT good's row is what closes this one.
        one = [VillageTrade("Sofrito", 400, {"Chili": 50})]
        two = one + [VillageTrade("Pulque", 663, {})]
        with mock.patch("memory.barter_kb.load_recipe", return_value=None):
            self.assertFalse(_target_complete(one, "Sofrito", "X"))
            self.assertTrue(_target_complete(two, "Sofrito", "X"))

    def test_a_good_not_yet_seen_is_never_complete(self):
        with mock.patch("memory.barter_kb.load_recipe", return_value=self._recipe()):
            self.assertFalse(_target_complete([VillageTrade("Pulque", 663, {"Coral": 1})],
                                              "Box of Nutmeg", "Melanesian Village"))
