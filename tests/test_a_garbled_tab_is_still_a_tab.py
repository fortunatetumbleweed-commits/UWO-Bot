"""A tab whose label OCR'd badly is still that tab.

Live 2026-08-29 the world map's Explore tab came back as 'EExplorgh' — a doubled first letter
and a lost last one. `'explore' in 'eexplorgh'` is False, so the strip read
['port', 'route', 'trade'], `select_world_map_tab('explore')` reported "not in the row", and
the remote check refused to read another tab's rail. That refusal was correct; the reading
underneath it was not. A Birch Tree run died at London on it, and EVERY village barter would
have, because the village list lives on that tab.

The strip already tolerated a MERGED label ('NarExplore' contains 'explore'). Containment does
nothing for a GARBLED one. OCR mangles ENDS far more than beginnings, so a prefix is the right
tolerance — and five characters of a tab name is still specific enough to be that tab.
"""
import types
import unittest
from unittest import mock

def _frame():
    """A real frame: the strip masks the tab band by brightness before parsing."""
    from PIL import Image
    return Image.new("RGB", (2400, 1080))


from actions.sail_actions import _world_map_tab_strip


def _el(label, cx):
    return types.SimpleNamespace(label=label, cx=cx, cy=55, y2=90)


def _strip(labels):
    """Run the detector over a scripted tab row."""
    els = [_el(lab, x) for lab, x in labels]
    parser = mock.MagicMock()
    parser.yolo_available.return_value = True
    parser.parse_fast.return_value = els
    with mock.patch("vision.omniparser.get_omniparser", return_value=parser):
        return [(n, cx) for n, cx, _cy, _y2 in _world_map_tab_strip(_frame())]


class NeitherTheWordNorTheBoxIsEnoughAlone(unittest.TestCase):
    """The fuzzy score says WHAT a label probably is; the box says WHERE it must be.

    Fuzzy alone: 'Explosives' scores 0.71 against 'explore' and 'Exp1oregh' scores 0.75 — a
    threshold between them is a coin toss, and one of them is not a tab.

    Box alone: something sits at every tab position on a busy map, so position without a word
    would claim whatever happens to be there.
    """

    def test_an_impostor_cannot_take_a_slot_it_does_not_fit(self):
        """'Explosives' scores well against 'explore' and is not a tab. It loses because
        taking the slot would put it out of order with the tabs around it."""
        got = dict(_strip([("Explosives", 400), ("Port", 859), ("Exploregh", 1052),
                           ("Route", 1235), ("Trade", 1424)]))
        self.assertEqual(got["explore"], 1052, "the impostor took Explore's slot")

    def test_the_order_is_what_rejects_it(self):
        """Same impostor, no real Explore on screen: it still must not be claimed, because
        400 is left of Port and Explore is right of it."""
        got = dict(_strip([("Explosives", 400), ("Port", 859),
                           ("Route", 1235), ("Trade", 1424)]))
        self.assertNotIn("explore", got)

    def test_the_best_TOTAL_assignment_wins_not_the_first_good_match(self):
        """A greedy left-to-right match would spend Explore on the first thing that scored,
        and leave the real tab unclaimed."""
        got = dict(_strip([("Port", 859), ("Explor", 1052), ("Exploregh", 1150),
                           ("Route", 1235), ("Trade", 1424)]))
        self.assertIn("explore", got)
        self.assertEqual([n for n, _ in sorted(got.items(), key=lambda kv: kv[1])],
                         ["port", "explore", "route", "trade"])


class AGarbledLabelIsStillItsTab(unittest.TestCase):
    def test_the_live_failure(self):
        """The exact row that killed the run."""
        got = _strip([("Port", 859), ("EExplorgh", 1045), ("Route", 1237), ("Trade", 1425)])
        self.assertEqual(got, [("port", 859), ("explore", 1045),
                               ("route", 1237), ("trade", 1425)])

    def test_a_merged_label_still_works(self):
        """The case containment was added for — it must not regress."""
        got = _strip([("Port", 859), ("NarExplore", 1045), ("Route", 1237)])
        self.assertIn(("explore", 1045), got)

    def test_a_clean_read_is_never_displaced_by_a_dirty_one(self):
        """Exact beats contains beats prefix, whatever order they arrive in."""
        got = _strip([("EExplorgh", 400), ("NarExplore", 800), ("Explore", 1045)])
        self.assertEqual(got, [("explore", 1045)])

    def test_a_dirty_read_is_used_when_it_is_all_there_is(self):
        self.assertEqual(_strip([("EExplorgh", 1045)]), [("explore", 1045)])

    def test_the_strip_stays_in_map_order(self):
        """`selected_tab_index` maps a highlight to a SLOT, so a missing tab shifts every
        index after it — which is how the highlight was read as the wrong tab."""
        got = _strip([("Trade", 1425), ("EExplorgh", 1045), ("Port", 859), ("Route", 1237)])
        self.assertEqual([n for n, _ in got], ["port", "explore", "route", "trade"])

    def test_something_that_is_not_a_tab_is_not_matched(self):
        """Tolerant, not credulous — and it is the BOX that draws that line, not the word."""
        got = _strip([("Explosives", 400), ("Portrait", 800)])
        self.assertNotIn("explore", [n for n, _ in got])


class TheROWIsTheThingThatCannotBeCorrupted(unittest.TestCase):
    """THE TAB BAR IS TRANSLUCENT (user, 2026-08-29).

    Map text underneath bleeds into the label, and what it does varies frame to frame: the
    same Explore tab read 'Exploregh' in one capture and 'Explorgh' in the next — both of them
    Edinbur-GH showing through. 'NarExplore', the merge this detector was first taught to
    handle, was the same thing from the other side.

    So no amount of string tolerance is reliable, because the corruption is not in the OCR,
    it is on the screen. What cannot be corrupted is the ROW: four tabs, known order, even
    spacing. A tab no label matched is a missed reading, and its position is arithmetic.
    """

    def test_a_tab_nothing_matched_is_found_by_its_place_in_the_row(self):
        """Bled so badly that even a prefix fails."""
        # 'Exp1oregh' — the l read as a 1, plus Edinbur-gh. No exact, no contains, no prefix.
        got = _strip([("Port", 859), ("Exp1oregh", 1052), ("Route", 1235), ("Trade", 1424)])
        self.assertEqual(got, [("port", 859), ("explore", 1052),
                               ("route", 1235), ("trade", 1424)])

    def test_the_live_readings_both_work(self):
        """The two captures taken minutes apart, on the same screen."""
        for label in ("Explorgh", "Exploregh"):
            got = dict(_strip([("Port", 859), (label, 1052),
                               ("Route", 1235), ("Trade", 1424)]))
            self.assertEqual(got.get("explore"), 1052, label)

    def test_it_does_not_invent_a_tab_where_none_sits(self):
        """Only an element actually AT the predicted place is claimed. A row that genuinely
        has three tabs stays three."""
        got = _strip([("Port", 859), ("Route", 1235), ("Trade", 1424)])
        self.assertEqual([n for n, _ in got], ["port", "route", "trade"])

    def test_one_neighbour_is_enough(self):
        """This asserted the opposite while the row was read by SPACING: two anchors were
        needed to know the pitch, so a two-tab row could not place anything.

        Scoring the word and ordering the box needs neither a pitch nor a third tab — the
        label says what it probably is, and sitting right of Port says it may be Explore.
        Recorded because the old limitation was a property of the method, not of the game.
        """
        got = dict(_strip([("Port", 859), ("Exp1oregh", 1052)]))
        self.assertEqual(got.get("explore"), 1052)

    def test_a_clean_label_still_wins_over_the_row(self):
        """Inference is the backstop, not the first answer."""
        got = dict(_strip([("Port", 859), ("Explore", 1052),
                           ("Route", 1235), ("Trade", 1424)]))
        self.assertEqual(got["explore"], 1052)
