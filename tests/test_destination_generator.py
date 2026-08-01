"""Tests for the destination generator.

The generator is a pure function — no perception, no I/O.  Tests cover
the three branches (fresh tangent / last-known / cold-start) plus
the geometry of the shore lookahead.
"""
import math
import unittest

from brain.goals.destination_generator import (
    ShoreTangent,
    generate_destination,
)


def _approx(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) < tol


class FreshShoreTangentTests(unittest.TestCase):
    """Branch 1: fresh tangent → shore-projected lookahead point."""

    def test_on_hug_line_returns_pure_forward_step(self):
        """When `d == d_star` the perpendicular nudge is zero and
        the destination is exactly `lookahead` along the tangent."""
        pos = (0.0, 0.0)
        tangent = ShoreTangent(
            tangent_compass_deg=0.0,    # heading due north
            perpendicular_distance=0.10,
            d_star=0.10,
        )
        lat, lon = generate_destination(
            pos=pos,
            heading_deg=0.0,
            shore_tangent=tangent,
            last_known_destination=None,
            hug_side="port",
            lookahead_distance=0.10,
        )
        # Pure north step: lat += 0.10, lon unchanged.
        self.assertTrue(_approx(lat, 0.10))
        self.assertTrue(_approx(lon, 0.00))

    def test_drift_off_line_produces_lateral_nudge(self):
        """When bot is wider than `d_star`, the destination is
        nudged perpendicular toward the shore (port side → west of
        a north-heading tangent)."""
        pos = (0.0, 0.0)
        tangent = ShoreTangent(
            tangent_compass_deg=0.0,    # north
            perpendicular_distance=0.20, # drifted out by 0.10
            d_star=0.10,
        )
        lat, lon = generate_destination(
            pos=pos,
            heading_deg=0.0,
            shore_tangent=tangent,
            last_known_destination=None,
            hug_side="port",
            lookahead_distance=0.10,
            lateral_gain=0.5,
        )
        # perp_compass = -90° (west) for port hug of a north tangent.
        # perp_offset = (0.20 - 0.10) * 0.5 = 0.05.
        # Δlat from perp term = 0.05 * cos(-90°) ≈ 0  (well, ~3e-18).
        # Δlon from perp term = 0.05 * sin(-90°) = -0.05.
        # Δlat from tangent = 0.10 * cos(0°) = 0.10.
        # Δlon from tangent = 0.10 * sin(0°) = 0.
        self.assertTrue(_approx(lat, 0.10, tol=1e-6))
        self.assertTrue(_approx(lon, -0.05, tol=1e-6))

    def test_starboard_nudges_to_the_other_side(self):
        """Same geometry, starboard hug → nudge east instead of west."""
        pos = (0.0, 0.0)
        tangent = ShoreTangent(
            tangent_compass_deg=0.0,
            perpendicular_distance=0.20,
            d_star=0.10,
        )
        lat, lon = generate_destination(
            pos=pos,
            heading_deg=0.0,
            shore_tangent=tangent,
            last_known_destination=None,
            hug_side="starboard",
            lookahead_distance=0.10,
            lateral_gain=0.5,
        )
        self.assertTrue(_approx(lat, 0.10, tol=1e-6))
        self.assertTrue(_approx(lon, +0.05, tol=1e-6))

    def test_east_tangent_steps_east(self):
        """Tangent compass=90° → lookahead step is to the east."""
        pos = (10.0, 20.0)
        tangent = ShoreTangent(
            tangent_compass_deg=90.0,
            perpendicular_distance=0.05,
            d_star=0.05,
        )
        lat, lon = generate_destination(
            pos=pos,
            heading_deg=90.0,
            shore_tangent=tangent,
            last_known_destination=None,
            hug_side="port",
            lookahead_distance=0.20,
        )
        # Pure east step from (10, 20): lat unchanged, lon += 0.20.
        self.assertTrue(_approx(lat, 10.0, tol=1e-6))
        self.assertTrue(_approx(lon, 20.20, tol=1e-6))


class LastKnownFallbackTests(unittest.TestCase):
    """Branch 2: stale tangent + last-known → return last-known."""

    def test_returns_last_known_verbatim(self):
        lat, lon = generate_destination(
            pos=(5.0, 5.0),
            heading_deg=180.0,
            shore_tangent=None,
            last_known_destination=(3.14, 2.72),
            hug_side="port",
        )
        self.assertEqual(lat, 3.14)
        self.assertEqual(lon, 2.72)

    def test_last_known_used_regardless_of_heading(self):
        """Heading is irrelevant in this branch — last-known wins."""
        for hdg in (0.0, 90.0, 180.0, 270.0):
            with self.subTest(heading=hdg):
                lat, lon = generate_destination(
                    pos=(0.0, 0.0),
                    heading_deg=hdg,
                    shore_tangent=None,
                    last_known_destination=(1.0, 2.0),
                    hug_side="starboard",
                )
                self.assertEqual((lat, lon), (1.0, 2.0))


class ColdStartFallbackTests(unittest.TestCase):
    """Branch 3: no tangent, no last-known → bow-forward synthetic."""

    def test_bow_forward_north(self):
        lat, lon = generate_destination(
            pos=(0.0, 0.0),
            heading_deg=0.0,        # north
            shore_tangent=None,
            last_known_destination=None,
            hug_side="port",
            lookahead_distance=0.10,
        )
        self.assertTrue(_approx(lat, 0.10))
        self.assertTrue(_approx(lon, 0.00))

    def test_bow_forward_east(self):
        lat, lon = generate_destination(
            pos=(0.0, 0.0),
            heading_deg=90.0,       # east
            shore_tangent=None,
            last_known_destination=None,
            hug_side="port",
            lookahead_distance=0.20,
        )
        self.assertTrue(_approx(lat, 0.00, tol=1e-9))
        self.assertTrue(_approx(lon, 0.20))

    def test_bow_forward_arbitrary_angle(self):
        pos = (10.0, 20.0)
        hdg = 45.0
        L = 0.10
        lat, lon = generate_destination(
            pos=pos,
            heading_deg=hdg,
            shore_tangent=None,
            last_known_destination=None,
            hug_side="port",
            lookahead_distance=L,
        )
        rad = math.radians(hdg)
        self.assertTrue(_approx(lat, 10.0 + L * math.cos(rad), tol=1e-9))
        self.assertTrue(_approx(lon, 20.0 + L * math.sin(rad), tol=1e-9))


class PriorityOrderTests(unittest.TestCase):
    """Tangent takes priority over last-known; both take priority
    over bow-forward."""

    def test_fresh_tangent_beats_last_known(self):
        """When both are present, the shore lookahead wins (fresh
        perception is more recent than last-known)."""
        pos = (0.0, 0.0)
        tangent = ShoreTangent(
            tangent_compass_deg=0.0,
            perpendicular_distance=0.05,
            d_star=0.05,
        )
        lat, lon = generate_destination(
            pos=pos,
            heading_deg=180.0,    # bow south, irrelevant
            shore_tangent=tangent,
            last_known_destination=(99.0, 99.0),    # would otherwise win
            hug_side="port",
            lookahead_distance=0.10,
        )
        # Pure north step from the tangent — not (99, 99).
        self.assertTrue(_approx(lat, 0.10))
        self.assertTrue(_approx(lon, 0.00))
        self.assertNotEqual((lat, lon), (99.0, 99.0))


if __name__ == "__main__":
    unittest.main()
