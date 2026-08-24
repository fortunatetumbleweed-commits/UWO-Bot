"""
Phase 5d tests — OmniParser-driven world map exploration.

Replaces the OCR-everything candidate enumeration in
explore_visible_ports_on_world_map.  Two pure-function tests anchor
the candidate filter against the May-2 incident's actual log
(article fragments, NPC chat, timestamps, panel content) plus a
clean world-map case with real port names.

The full explore_visible_ports_on_world_map function is also tested
end-to-end with mocks for capture, perceive, OmniParser, tap, and
read_city_info_panel.
"""

import unittest
from unittest.mock import MagicMock, patch

from vision.omniparser import DetectedElement


def _text_el(text, cx, cy):
    """Helper: build a DetectedElement at a centre position."""
    half = 30
    return DetectedElement(
        label=text, element_type="text",
        x1=cx - half, y1=cy - 10,
        x2=cx + half, y2=cy + 10,
        confidence=0.85,
    )


# Real game port names (from the port catalogue)
_KNOWN_PORTS = [
    "london", "dover", "amsterdam", "lisbon", "seville",
    "plymouth", "santiago_de_cuba",
]


class FindWorldMapCityCandidatesTests(unittest.TestCase):
    """Pure-function tests of the candidate filter."""

    def test_real_port_names_pass_kb_match(self):
        from actions.world_map import find_world_map_city_candidates

        elements = [
            _text_el("London",    400, 200),
            _text_el("Plymouth",  450, 250),
            _text_el("Lisbon",    500, 300),
        ]
        candidates = find_world_map_city_candidates(
            elements, port_kb=_KNOWN_PORTS,
        )
        names = [c[0] for c in candidates]
        self.assertIn("london", names)
        self.assertIn("plymouth", names)
        self.assertIn("lisbon", names)

    def test_may_2_article_fragments_rejected(self):
        """The exact strings from the May-2 16:04 log must NOT be
        treated as port-name candidates."""
        from actions.world_map import find_world_map_city_candidates

        garbage = [
            _text_el("Tne dig one IS",        1467, 54),
            _text_el("Britain!",              1423, 82),   # has '!'
            _text_el("need to build a lot:",  635, 174),   # has ':' + lowercase
            _text_el("00.00-23.59",           1279, 363),  # digits
            _text_el("Buy and sell Trade Goods:", 1259, 411),  # ':' + sentence
            _text_el("We hired a",            1489, 687),  # x in panel region
            _text_el("accountan",             1489, 723),  # x in panel region
            _text_el("company:",              1484, 759),  # ':' + panel
            _text_el("trading is",            1483, 792),  # lowercase + panel
            _text_el("easier:",               1462, 828),  # ':' + panel
            _text_el("TaylorFP",              1233, 505),  # in panel
        ]
        candidates = find_world_map_city_candidates(
            garbage, port_kb=_KNOWN_PORTS,
        )
        self.assertEqual(candidates, [],
                         f"Garbage candidates leaked through: {candidates}")

    def test_panel_region_excludes_real_port_names_too(self):
        """A real port name inside the right-panel region (x >= 1500) is
        skipped — it's panel content, not a map label."""
        from actions.world_map import find_world_map_city_candidates

        elements = [
            _text_el("London", 1700, 200),  # inside panel — skip
            _text_el("Lisbon", 400, 300),   # on map — keep
        ]
        candidates = find_world_map_city_candidates(
            elements, port_kb=_KNOWN_PORTS,
        )
        names = [c[0] for c in candidates]
        self.assertNotIn("london", names)
        self.assertIn("lisbon", names)

    def test_unknown_port_passes_shape_filter(self):
        """A genuinely-novel port name (not in KB yet) should still be
        accepted via the shape filter."""
        from actions.world_map import find_world_map_city_candidates

        elements = [_text_el("Marrakesh", 400, 200)]
        candidates = find_world_map_city_candidates(
            elements, port_kb=_KNOWN_PORTS,
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0][0], "Marrakesh")

    def test_compound_port_name_with_hyphen_accepted(self):
        from actions.world_map import find_world_map_city_candidates

        elements = [_text_el("Saint-Malo", 400, 200)]
        candidates = find_world_map_city_candidates(
            elements, port_kb=_KNOWN_PORTS,
        )
        self.assertEqual(candidates[0][0], "Saint-Malo")

    def test_lowercase_word_rejected_by_shape_filter(self):
        """Even when not in KB, all-lowercase words don't pass the
        proper-noun shape filter."""
        from actions.world_map import find_world_map_city_candidates

        elements = [_text_el("trading", 400, 200)]
        candidates = find_world_map_city_candidates(
            elements, port_kb=_KNOWN_PORTS,
        )
        self.assertEqual(candidates, [])

    def test_world_map_control_labels_rejected(self):
        """Top-center mode tabs and bottom action buttons are title-case
        but are NOT port candidates."""
        from actions.world_map import find_world_map_city_candidates

        controls = [
            _text_el("Port",                   1180, 30),  # mode tab
            _text_el("Explore",                1300, 30),
            _text_el("Route",                  1420, 30),
            _text_el("Trade",                  1540, 30),  # also outside
            _text_el("World Map",              140,  30),  # screen title
            _text_el("Go to City",             1170, 970),
            _text_el("Trade Event Schedule",   130,  810),
            _text_el("My Location",            120,  870),
            _text_el("Filter",                 240,  870),
            _text_el("Enemy Company LV",       170,  870),
        ]
        candidates = find_world_map_city_candidates(
            controls, port_kb=_KNOWN_PORTS,
        )
        self.assertEqual(candidates, [],
                         f"Control labels leaked through: {candidates}")

    def test_unvisited_port_question_marks_rejected(self):
        """Unvisited cities render as '???'.  They're tappable in other
        contexts but City Info won't open for them, so they're not
        useful candidates for explore_visible_ports."""
        from actions.world_map import find_world_map_city_candidates

        elements = [_text_el("???", 600, 300)]
        candidates = find_world_map_city_candidates(
            elements, port_kb=_KNOWN_PORTS,
        )
        self.assertEqual(candidates, [])

    def test_duplicates_deduplicated(self):
        from actions.world_map import find_world_map_city_candidates

        elements = [
            _text_el("London", 400, 200),
            _text_el("London", 600, 250),  # second sighting — same label
        ]
        candidates = find_world_map_city_candidates(
            elements, port_kb=_KNOWN_PORTS,
        )
        self.assertEqual(len(candidates), 1)


class ExploreVisiblePortsTests(unittest.TestCase):
    """End-to-end tests with mocked capture, OmniParser, perceive, tap."""

    def test_aborts_when_not_on_world_map(self):
        from actions.world_map import explore_visible_ports_on_world_map

        fake_pr = MagicMock(state="port_overworld",
                            detail="At Plymouth")
        fake_frame = MagicMock()

        with patch("capture.adb_capture.capture_screen",
                   return_value=fake_frame), \
             patch("brain.perceive.perceive", return_value=fake_pr), \
             patch("actions.adb_actions.tap") as mock_tap, \
             patch("vision.omniparser.get_omniparser") as MockGet:
            result = explore_visible_ports_on_world_map()
            self.assertEqual(result, [])
            mock_tap.assert_not_called()
            # OmniParser also not invoked — we bailed before any work
            MockGet.return_value.parse_fast.assert_not_called()

    def test_no_save_when_panel_absent_after_tap(self):
        """Tapping a candidate but not getting a City Info panel back
        means the candidate was a false positive — don't save garbage."""
        from actions.world_map import explore_visible_ports_on_world_map

        fake_pr = MagicMock(state="world_map", detail="World map open")
        fake_frame = MagicMock()

        with patch("capture.adb_capture.capture_screen",
                   return_value=fake_frame), \
             patch("brain.perceive.perceive", return_value=fake_pr), \
             patch("vision.omniparser.get_omniparser") as MockGet, \
             patch("vision.omniparser.parse_fast_cached",
                   return_value=[_text_el("London", 400, 200)]), \
             patch("actions.adb_actions.tap"), \
             patch("actions.world_map.read_city_info_panel",
                   return_value=None) as mock_read, \
             patch("actions.world_map.save_city_info_to_kb") as mock_save, \
             patch("time.sleep"):
            MockGet.return_value.yolo_available.return_value = True
            result = explore_visible_ports_on_world_map()
            self.assertEqual(result, [])
            mock_read.assert_called()
            mock_save.assert_not_called()

    def test_saves_when_panel_appears(self):
        from actions.world_map import explore_visible_ports_on_world_map, CityInfoPanel

        fake_pr = MagicMock(state="world_map", detail="World map open")
        fake_frame = MagicMock()
        panel = CityInfoPanel(port="London", goods=["Wool"])

        with patch("capture.adb_capture.capture_screen",
                   return_value=fake_frame), \
             patch("brain.perceive.perceive", return_value=fake_pr), \
             patch("vision.omniparser.get_omniparser") as MockGet, \
             patch("vision.omniparser.parse_fast_cached",
                   return_value=[_text_el("London", 400, 200)]), \
             patch("actions.adb_actions.tap"), \
             patch("actions.world_map.read_city_info_panel",
                   return_value=panel), \
             patch("actions.world_map.save_city_info_to_kb") as mock_save, \
             patch("time.sleep"):
            MockGet.return_value.yolo_available.return_value = True
            result = explore_visible_ports_on_world_map()
            self.assertEqual(result, ["London"])
            mock_save.assert_called_once()

    def test_aborts_when_omniparser_unavailable(self):
        """No silent fallback to the legacy OCR-everything path — we
        retired it in Phase 5d.  If OmniParser isn't available, abort."""
        from actions.world_map import explore_visible_ports_on_world_map

        fake_pr = MagicMock(state="world_map", detail="World map open")
        fake_frame = MagicMock()

        with patch("capture.adb_capture.capture_screen",
                   return_value=fake_frame), \
             patch("brain.perceive.perceive", return_value=fake_pr), \
             patch("vision.omniparser.get_omniparser") as MockGet, \
             patch("actions.adb_actions.tap") as mock_tap:
            MockGet.return_value.yolo_available.return_value = False
            result = explore_visible_ports_on_world_map()
            self.assertEqual(result, [])
            mock_tap.assert_not_called()


if __name__ == "__main__":
    unittest.main()
