"""Tests for vision/scene_layouts.py — the loader that maps
(nav_state, detail, parent_building) to a concatenated markdown prompt.

Tests work against the actual files in memory/knowledge/scene_layouts/
so the loader is exercised end-to-end on real content.  When new
layout files are added, these tests verify the lookup chain still
resolves them correctly.
"""

import unittest

from vision.scene_layouts import (
    load_layout, list_layouts, _parse_detail, _slug,
    _canonical_building,
)


class SlugTests(unittest.TestCase):

    def test_lowercase(self):
        self.assertEqual(_slug("Harbor"), "harbor")

    def test_whitespace_to_underscore(self):
        self.assertEqual(_slug("Recruit Crew"), "recruit_crew")

    def test_strip(self):
        self.assertEqual(_slug("  Inn  "), "inn")

    def test_none(self):
        self.assertIsNone(_slug(None))

    def test_empty(self):
        self.assertIsNone(_slug(""))


class ParseDetailTests(unittest.TestCase):

    def test_building_simple(self):
        b, sm = _parse_detail("building", "building: harbor")
        self.assertEqual(b, "harbor")
        self.assertIsNone(sm)

    def test_building_with_extra(self):
        b, sm = _parse_detail("building", "building: harbor — some Qwen prose")
        self.assertEqual(b, "harbor")

    def test_sub_menu(self):
        b, sm = _parse_detail("sub_menu", "sub_menu: recruit crew")
        self.assertIsNone(b)
        self.assertEqual(sm, "recruit_crew")

    def test_no_colon(self):
        b, sm = _parse_detail("port_overworld", "no colon detail")
        self.assertIsNone(b)
        self.assertIsNone(sm)


class LoadLayoutTests(unittest.TestCase):
    """Exercise the lookup chain against the real KB files."""

    def test_port_overworld_returns_content(self):
        out = load_layout("port_overworld", detail=None)
        self.assertIn("port_overworld", out.lower())
        # Should mention the right-panel structure
        self.assertIn("right edge", out.lower())

    def test_unknown_nav_state_still_returns_universal(self):
        # Universal chrome is loaded for every nav_state, even unknown
        # ones — bot's phone OS + server UID at the bottom are always
        # on screen.
        out = load_layout("never_seen_state")
        self.assertIn("Universal chrome", out)
        self.assertIn("server name", out.lower())

    def test_universal_chrome_in_port_overworld_load(self):
        out = load_layout("port_overworld")
        # Universal section is prepended
        self.assertIn("Universal chrome", out)
        # And the port_overworld content follows
        self.assertIn("Right edge", out)

    def test_building_chain_includes_default_and_specific(self):
        # Harbor file exists; chain should produce both _default and harbor
        out = load_layout("building", detail="building: harbor")
        self.assertIn("Back arrow", out)       # from _default.md
        self.assertIn("Harbor", out)            # from harbor/_building.md

    def test_building_with_unknown_building_falls_back_to_default(self):
        # No layout file for 'made_up_building'; default chrome should
        # still be returned.
        out = load_layout("building", detail="building: made_up_building")
        self.assertIn("Back arrow", out)
        # No building-specific section
        self.assertNotIn("Harbor — main view", out)
        self.assertNotIn("Inn — main view", out)

    def test_submenu_with_parent_chains_three_files(self):
        out = load_layout("sub_menu",
                            detail="sub_menu: recruit crew",
                            parent_building="harbor")
        # Should contain content from _default.md, harbor/_building.md,
        # and harbor/recruit_crew.md
        self.assertIn("Back arrow", out)
        self.assertIn("Harbor", out)
        self.assertIn("Quantity stepper", out)   # recruit_crew specifics

    def test_submenu_without_parent_falls_back_to_default(self):
        out = load_layout("sub_menu", detail="sub_menu: recruit crew",
                            parent_building=None)
        self.assertIn("Back arrow", out)
        # Without a parent, no building-specific content
        self.assertNotIn("Harbor — main view", out)
        self.assertNotIn("Quantity stepper", out)

    def test_submenu_unknown_submenu_falls_back_to_building(self):
        out = load_layout("sub_menu",
                            detail="sub_menu: never_seen_menu",
                            parent_building="harbor")
        # Still includes default + harbor _building, just not the
        # submenu-specific layer
        self.assertIn("Back arrow", out)
        self.assertIn("Harbor", out)
        self.assertNotIn("Quantity stepper", out)

    def test_separator_used_between_sections(self):
        out = load_layout("building", detail="building: harbor")
        # The loader joins parts with the separator
        self.assertIn("---", out)

    def test_inn_recruit_crew_chain(self):
        # Sibling case: same lookup pattern for Inn
        out = load_layout("sub_menu",
                            detail="sub_menu: recruit crew",
                            parent_building="inn")
        self.assertIn("Inn", out)
        self.assertIn("Fleet Crew Size", out)
        # Sibling-comment marker should appear
        self.assertIn("identical to", out.lower())


class BuildingAliasTests(unittest.TestCase):
    """Building-name aliases — palace/castle, cathedral/temple/sanctuary/church."""

    def test_canonical_passthrough_for_already_canonical(self):
        self.assertEqual(_canonical_building("harbor"), "harbor")
        self.assertEqual(_canonical_building("castle"), "castle")
        self.assertEqual(_canonical_building("church"), "church")

    def test_palace_resolves_to_castle(self):
        self.assertEqual(_canonical_building("palace"), "castle")

    def test_cathedral_temple_sanctuary_all_resolve_to_church(self):
        for variant in ("cathedral", "temple", "sanctuary"):
            self.assertEqual(_canonical_building(variant), "church",
                             f"{variant} should alias to church")

    def test_unknown_passes_through(self):
        self.assertEqual(_canonical_building("estate"), "estate")
        self.assertEqual(_canonical_building("never_seen"), "never_seen")

    def test_none_returns_none(self):
        self.assertIsNone(_canonical_building(None))

    def test_load_layout_for_palace_uses_castle_file(self):
        # buildings/palace/ does NOT exist; buildings/castle/_building.md does.
        # The alias map should redirect the lookup.
        out_palace = load_layout("building", detail="building: palace")
        out_castle = load_layout("building", detail="building: castle")
        # Both should pull the same building-specific section (the
        # castle/_building.md TODO stub) on top of _default.md.
        # We can't assert exact string equality (universal preamble may
        # share content), but the building-section content should match.
        self.assertIn("castle", out_palace.lower())
        self.assertIn("castle", out_castle.lower())

    def test_load_layout_for_temple_uses_church_file(self):
        out_temple = load_layout("building", detail="building: temple")
        # Should load buildings/church/_building.md (the canonical stub)
        # Without the alias, it would look for buildings/temple/_building.md
        # which doesn't exist.
        self.assertIn("church", out_temple.lower())


class ListLayoutsTests(unittest.TestCase):

    def test_lists_expected_files(self):
        layouts = list_layouts()
        # Spot-check a few we know exist
        self.assertIn("port_overworld.md", layouts)
        self.assertIn("buildings/_default.md", layouts)
        self.assertIn("buildings/harbor/_building.md", layouts)
        self.assertIn("buildings/harbor/recruit_crew.md", layouts)
        self.assertIn("buildings/inn/_building.md", layouts)
        self.assertIn("buildings/inn/recruit_crew.md", layouts)
        # No README.md assertion: memory/knowledge/scene_layouts/ has never contained
        # one (it is untracked in git history), and a README would be documentation
        # rather than a scene layout, so listing it is not something to require.


class SiblingMarkerConsistencyTests(unittest.TestCase):
    """The duplicate-with-comment convention: when two files are flagged
    as identical, both should carry the marker."""

    def test_recruit_crew_siblings_both_marked(self):
        layouts = list_layouts()
        harbor_rc = layouts["buildings/harbor/recruit_crew.md"]
        inn_rc    = layouts["buildings/inn/recruit_crew.md"]
        # Both should mention the other in their sibling comment
        self.assertIn("buildings/inn/recruit_crew.md", harbor_rc)
        self.assertIn("buildings/harbor/recruit_crew.md", inn_rc)


if __name__ == "__main__":
    unittest.main()
