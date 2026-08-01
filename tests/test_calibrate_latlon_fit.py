"""Tests for the affine fit in tools/calibrate_latlon.py.

The interactive parts (capture, input) are not tested — those need a
running game.  Only the pure-math fit is unit-tested here, to catch
sign errors and shape mistakes before the user runs the tool.
"""
import sys
import unittest
from pathlib import Path

# Make tools/ importable as a package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.calibrate_latlon import _fit_affine, _loo_cross_validate


def _synthetic_samples(a, b, c, d, e, f, points):
    """Build samples that EXACTLY satisfy cat_x = a*lon + b*lat + c, etc.
    A good fit must recover (a, b, c, d, e, f) with near-zero residuals."""
    samples = []
    for port, lat, lon in points:
        cx = a * lon + b * lat + c
        cy = d * lon + e * lat + f
        samples.append({
            "port": port, "lat": lat, "lon": lon,
            "cat_x": cx, "cat_y": cy,
        })
    return samples


class AffineFitTests(unittest.TestCase):

    def test_recovers_known_transform_3_points(self):
        # Real-ish coefficients: lon → x is positive, lat → y is negative
        # (north has smaller y in image coords).
        points = [
            ("lisbon",  40.19,  -9.91),
            ("tripoli", 32.90,  13.20),
            ("london",  51.50,  -0.10),
        ]
        samples = _synthetic_samples(
            a=96.0, b=2.0, c=3700.0,
            d=-3.0, e=-41.0, f=3500.0,
            points=points,
        )
        fit = _fit_affine(samples)
        t = fit["transform"]
        self.assertAlmostEqual(t["a"],  96.0, places=2)
        self.assertAlmostEqual(t["b"],   2.0, places=2)
        self.assertAlmostEqual(t["c"], 3700.0, places=1)
        self.assertAlmostEqual(t["d"],  -3.0, places=2)
        self.assertAlmostEqual(t["e"], -41.0, places=2)
        self.assertAlmostEqual(t["f"], 3500.0, places=1)
        self.assertLess(fit["rms_error_px"], 0.01)

    def test_overdetermined_fit_4_points_clean(self):
        # 4 points exactly on the same affine → still recovers it.
        points = [
            ("lisbon",  40.19,  -9.91),
            ("tripoli", 32.90,  13.20),
            ("london",  51.50,  -0.10),
            ("calicut", 11.25,  75.78),
        ]
        samples = _synthetic_samples(
            a=96.0, b=0.0, c=3700.0,
            d=0.0, e=-41.0, f=3500.0,
            points=points,
        )
        fit = _fit_affine(samples)
        self.assertLess(fit["rms_error_px"], 0.01)
        # All residuals near zero.
        for r in fit["residuals"]:
            self.assertLess(r["error_px"], 0.1)

    def test_loo_with_5_points(self):
        points = [
            ("a", 10.0, 10.0),
            ("b", 20.0, 30.0),
            ("c",  5.0, -5.0),
            ("d", 40.0, 60.0),
            ("e", -10.0, 20.0),
        ]
        samples = _synthetic_samples(
            a=100.0, b=0.0, c=0.0,
            d=0.0, e=-50.0, f=0.0,
            points=points,
        )
        loo = _loo_cross_validate(samples)
        self.assertEqual(len(loo), 5)
        # All errors near zero — the transform is exactly an affine.
        for r in loo:
            self.assertLess(r["loo_error_px"], 0.1)

    def test_loo_returns_one_entry_per_sample(self):
        # Smoke test: LOO output shape matches input size, each entry
        # has the expected keys.  Outlier detection via LOO is brittle
        # with small sample counts (a single bad point has high leverage
        # on the remaining LS fit), so we don't try to assert it here —
        # the printed residuals are for the user's eye, not algorithmic
        # filtering.
        points = [
            ("a", 10.0, 10.0),
            ("b", 20.0, 30.0),
            ("c",  5.0, -5.0),
            ("d", 40.0, 60.0),
            ("e", 25.0, 15.0),
        ]
        samples = _synthetic_samples(
            a=100.0, b=0.0, c=0.0,
            d=0.0, e=-50.0, f=0.0,
            points=points,
        )
        loo = _loo_cross_validate(samples)
        self.assertEqual(len(loo), 5)
        for r in loo:
            self.assertIn("port", r)
            self.assertIn("loo_error_px", r)

    def test_loo_empty_for_under_4_samples(self):
        samples = _synthetic_samples(
            a=1.0, b=0.0, c=0.0, d=0.0, e=1.0, f=0.0,
            points=[("a", 1.0, 2.0), ("b", 3.0, 4.0), ("c", 5.0, 6.0)],
        )
        self.assertEqual(_loo_cross_validate(samples), [])


if __name__ == "__main__":
    unittest.main()
