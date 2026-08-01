"""Tests for `MinimapNavigationView.village_overlap`.

The property fires when any detected target (village/port icon) is
within `VILLAGE_OVERLAP_RADIUS_PX` of the ship's centroid — the
signal that local water mask, sectors, skeleton tangent, AND HUD
lat/lon are all corrupted by the village's icon + label overlay.

See `memory/project_hud_pirate_sprite_occlusion.md` and the in-line
docstring on the property for the perception mechanism.
"""
import unittest
from unittest.mock import MagicMock

from vision.minimap_navigation_view import (
    MinimapNavigationView,
    VILLAGE_OVERLAP_RADIUS_PX,
    _Reading,
)
from vision.navigation_view import Target


def _make_nav(ship_xy, targets_and_positions):
    """Build a `MinimapNavigationView` with stub targets and positions."""
    targets = tuple(t for t, _pos in targets_and_positions)
    target_pos = {id(t): pos for t, pos in targets_and_positions}
    return MinimapNavigationView(_Reading(
        ship_xy=ship_xy,
        heading=180.0,
        sectors=(),
        targets=targets,
        target_pos=target_pos,
    ))


class VillageOverlapTests(unittest.TestCase):

    def test_no_ship_xy_returns_false(self):
        nav = _make_nav(ship_xy=None, targets_and_positions=[])
        self.assertFalse(nav.village_overlap)

    def test_no_targets_returns_false(self):
        nav = _make_nav(ship_xy=(100.0, 50.0), targets_and_positions=[])
        self.assertFalse(nav.village_overlap)

    def test_target_far_from_ship_returns_false(self):
        target = Target(kind="port_known", bearing_deg=0.0, distance=0.5,
                        name="Cairo")
        # Ship at (100, 50), target at (300, 50) → 200 px away.
        nav = _make_nav(ship_xy=(100.0, 50.0),
                        targets_and_positions=[(target, (300.0, 50.0))])
        self.assertFalse(nav.village_overlap)

    def test_target_within_radius_returns_true(self):
        target = Target(kind="port_known", bearing_deg=0.0, distance=0.1,
                        name="Bari")
        # Ship at (100, 50), target at (120, 60) → ~22 px away ≤ 40 px.
        nav = _make_nav(ship_xy=(100.0, 50.0),
                        targets_and_positions=[(target, (120.0, 60.0))])
        self.assertTrue(nav.village_overlap)

    def test_target_exactly_at_radius_returns_true(self):
        target = Target(kind="port_known", bearing_deg=0.0, distance=0.1,
                        name="V")
        # Ship at origin, target exactly VILLAGE_OVERLAP_RADIUS_PX away.
        nav = _make_nav(ship_xy=(0.0, 0.0),
                        targets_and_positions=[
                            (target, (float(VILLAGE_OVERLAP_RADIUS_PX), 0.0))])
        self.assertTrue(nav.village_overlap)

    def test_one_close_one_far_target_returns_true(self):
        """Any one close target triggers overlap — far ones don't mask it."""
        close = Target(kind="port_known", bearing_deg=0.0, distance=0.1,
                       name="close")
        far   = Target(kind="port_unvisited", bearing_deg=180.0, distance=0.9,
                       name=None)
        nav = _make_nav(
            ship_xy=(100.0, 50.0),
            targets_and_positions=[
                (close, (110.0, 60.0)),
                (far,   (400.0, 200.0)),
            ],
        )
        self.assertTrue(nav.village_overlap)


if __name__ == "__main__":
    unittest.main()
