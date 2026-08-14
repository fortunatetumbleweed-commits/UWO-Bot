"""World-map globe wrap: shortest horizontal delta across the date-line seam."""
import unittest
import actions.world_map_nav as w


class WrapTests(unittest.TestCase):
    def test_wrap_period_from_affine(self):
        self.assertTrue(6000 < w._world_wrap_gx() < 16000)

    def test_far_east_west_takes_the_wrap(self):
        # Nagasaki(x=8270) -> Port Royal(x=2504): direct -5766 (long way west);
        # the short way is EAST across the Pacific (positive).
        self.assertGreater(w._wrap_dx(2504 - 8270), 0)
        self.assertLess(abs(w._wrap_dx(2504 - 8270)), 5766)

    def test_atlantic_routes_unchanged(self):
        # London(4680) -> Port Royal(2504) and -> Amsterdam(4807): no wrap.
        self.assertEqual(round(w._wrap_dx(2504 - 4680)), -2176)
        self.assertEqual(round(w._wrap_dx(4807 - 4680)), 127)

    def test_wrap_dx_is_within_half_period(self):
        W = w._world_wrap_gx()
        for dgx in (-9000, -100, 0, 3000, 9000, 20000):
            self.assertLessEqual(abs(w._wrap_dx(dgx)), W / 2 + 1)


if __name__ == "__main__":
    unittest.main()
