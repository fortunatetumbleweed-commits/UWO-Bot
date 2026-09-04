"""One merged quantity badge must not discard every name on the screen.

OmniParser occasionally returns a row's quantity badge fused with the category chip beside
it — a ~106px box comes back 420px wide. `_row_names` used the WIDEST badge to decide where
names begin, so that single box pushed the boundary past every name on screen and
`parse_trade_list` returned nothing while the rows were perfectly visible and perfectly
OCR'd. Live at San Village 2026-08-25: Prunus Padus, Rhino Horn, Senna and Anise all lost,
and Prunus Padus' recipe was stored empty.
"""

from __future__ import annotations

import types
import unittest

from actions.village_check import parse_trade_list


def _el(label, x1, y1, x2, y2, etype="text"):
    return types.SimpleNamespace(label=label, element_type=etype, x1=x1, y1=y1, x2=x2, y2=y2,
                                 cx=(x1 + x2) // 2, cy=(y1 + y2) // 2, confidence=1.0)


def _screen(*, merged: bool):
    """Prunus Padus and its three materials, as measured on the device."""
    pig_badge = _el("285", 1772, 427, 2192 if merged else 1828, 482)
    return [
        pig_badge,
        _el("Pig", 1829, 391, 1913, 431),
        _el("846", 1695, 498, 1830, 628, "button"),
        _el("Prunus Padus", 1833, 507, 2045, 543),
        _el("170", 1782, 708, 1822, 732),
        _el("Rhino Horn", 1827, 631, 2010, 690),
        _el("170", 1782, 820, 1822, 844),
        _el("Senna", 1827, 759, 1941, 800),
        _el("170", 1783, 933, 1821, 953),
        _el("Anise", 1831, 871, 1955, 907),
    ]


class AMergedBadgeMustNotBlankTheScreen(unittest.TestCase):

    def test_names_survive_a_420px_badge(self):
        """The screen parses the same whether or not one badge came back merged."""
        got = parse_trade_list(_screen(merged=True))
        self.assertTrue(got, "a single merged badge discarded every name on the screen")
        names = {t.good for t in got} | {m for t in got for m in t.materials}
        self.assertIn("Prunus Padus", names)

    def test_the_merged_screen_reads_like_the_clean_one(self):
        clean = parse_trade_list(_screen(merged=False))
        merged = parse_trade_list(_screen(merged=True))
        self.assertEqual([(t.good, dict(t.materials)) for t in clean],
                         [(t.good, dict(t.materials)) for t in merged])


if __name__ == "__main__":
    unittest.main()


# OmniParser also merges the OTHER way: instead of a tight box around the name text, it
# returns the WHOLE ROW CARD as one box and hangs the row's name on it. The label is then
# anchored at the row's left edge (~1700) rather than the text's (~1830). Both the name
# filter and the name/badge pairing asked whether a name STARTS right of the badge column,
# so every name on such a screen was discarded — live at Cheyenne 2026-08-25 the screen
# holding Moccasin, American Bison, Wool and Bullet parsed as EMPTY with all four detected
# at 0.89-1.00 confidence, and Moccasin never reached the knowledge base at all.

def _row_card_screen():
    """Cheyenne's Moccasin screen, as OmniParser actually returned it."""
    return [
        _el("150", 1783, 489, 1821, 509),
        _el("Bullet", 1693, 395, 2219, 531, "button"),
        _el("769", 1772, 630, 1824, 660),
        _el("Moccasin", 1695, 531, 2217, 670, "button"),
        _el("300", 1781, 749, 1821, 769),
        _el("American Bison", 1711, 671, 2215, 783, "button"),
        _el("340", 1781, 861, 1821, 881),
        _el("Wool", 1711, 786, 2215, 897, "button"),
        # The location pins that mark a row as a MATERIAL. Moccasin is the GOOD here and
        # carries none, exactly as on the device.
        _el("icon", 2162, 469, 2208, 517, "icon"),      # Bullet
        _el("icon", 2162, 730, 2208, 778, "icon"),      # American Bison
        _el("icon", 2161, 841, 2207, 889, "icon"),      # Wool
    ]


class AWholeRowBoxStillNamesItsRow(unittest.TestCase):

    def test_the_screen_is_not_empty(self):
        got = parse_trade_list(_row_card_screen())
        self.assertTrue(got, "every name was discarded for starting left of the badges")

    def test_moccasin_keeps_its_materials(self):
        got = {t.good: dict(t.materials) for t in parse_trade_list(_row_card_screen())}
        self.assertIn("Moccasin", got)
        self.assertEqual(got["Moccasin"], {"American Bison": 300, "Wool": 340})

    def test_a_far_off_overlay_is_still_not_a_name(self):
        """The amity tooltip that once became a material sat 406px clear of the badge."""
        screen = _row_card_screen() + [_el("Amity increases by", 2230, 620, 2390, 660)]
        got = {t.good: dict(t.materials) for t in parse_trade_list(screen)}
        self.assertNotIn("Amity increases by", got.get("Moccasin", {}))


# A row carries THREE signals of good-versus-material — the location pin, the indent, and the
# row height — and each fails differently. The pin leads because it is the only one that
# survives a continuation screen, where every row is a material: there the indent baseline is
# itself a material and the height spread vanishes, so geometry alone would call the whole
# screen goods. But a pin that goes UNDETECTED promotes a material to a good, and a good with
# another good's materials beneath it is a wrong recipe — at Svear on 2026-08-25 that made
# goods out of Birch Tree and Iron.

def _row(name, qty, y, *, indented, pin, h=133):
    """A row as OmniParser returns it: thumbnail, name, and (for a material) a pin."""
    x1 = 1722 if indented else 1694
    els = [_el(str(qty), x1, y, x1 + 106, y + h, "button"),
           _el(name, 1830, y + 20, 1990, y + 56)]
    if pin:
        els.append(_el("icon", 2162, y + 40, 2208, y + 88, "icon"))
    return els


class ThreeSignalsDecideGoodOrMaterial(unittest.TestCase):

    def test_a_material_whose_pin_was_missed_is_still_a_material(self):
        """Indented AND short, with no pin: the geometry rescues it."""
        els = (_row("Naverslojd", 769, 390, indented=False, pin=False, h=133)
               + _row("Birch Tree", 340, 530, indented=True, pin=False, h=105)
               + _row("Iron", 170, 640, indented=True, pin=True, h=105))
        got = {t.good: dict(t.materials) for t in parse_trade_list(els)}
        self.assertEqual(got.get("Naverslojd"), {"Birch Tree": 340, "Iron": 170},
                         "a missed pin turned a material into a good")

    def test_a_good_is_not_demoted_by_a_missing_pin(self):
        """Flush-left and tall: a good has no pin and must stay a good."""
        els = (_row("Birch Tree", 413, 390, indented=False, pin=False, h=133)
               + _row("Iron", 119, 530, indented=True, pin=True, h=105)
               + _row("Naverslojd", 769, 640, indented=False, pin=False, h=133))
        self.assertEqual([t.good for t in parse_trade_list(els)],
                         ["Birch Tree", "Naverslojd"])

    def test_a_continuation_screen_is_all_materials(self):
        """Every row a material: the indent baseline IS a material and the heights match, so
        geometry would call them all goods. The pin must still win."""
        els = (_row("Compass", 170, 390, indented=True, pin=True, h=105)
               + _row("Chorong", 170, 500, indented=True, pin=True, h=105)
               + _row("Olive Oil", 150, 610, indented=True, pin=True, h=105))
        trades = parse_trade_list(els)
        self.assertEqual([t.good for t in trades], [""], "these are continuation materials")
        self.assertEqual(dict(trades[0].materials),
                         {"Compass": 170, "Chorong": 170, "Olive Oil": 150})
