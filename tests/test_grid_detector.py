"""Grid/list detector — structural regularity from element bboxes."""
import unittest
from types import SimpleNamespace

from vision.grid_detector import detect_grid


def _el(x1, y1, x2, y2, etype="button", label=""):
    return SimpleNamespace(
        x1=x1, y1=y1, x2=x2, y2=y2, cx=(x1 + x2) / 2, cy=(y1 + y2) / 2,
        element_type=etype, label=label,
    )


def _make_grid(rows, cols, w=450, h=228, x0=460, y0=190, gap_x=10, gap_y=12):
    els = []
    for r in range(rows):
        for c in range(cols):
            x1 = x0 + c * (w + gap_x)
            y1 = y0 + r * (h + gap_y)
            els.append(_el(x1, y1, x1 + w, y1 + h, label=f"item{r}{c}"))
    return els


class DetectGridTests(unittest.TestCase):
    def test_3x3_grid(self):
        els = _make_grid(3, 3)
        g = detect_grid(els, 2400, 1080, cell_types=("button",))
        self.assertIsNotNone(g)
        self.assertEqual((g.n_rows, g.n_cols), (3, 3))
        self.assertEqual(len(g.cells), 9)
        self.assertFalse(g.is_list)

    def test_2x3_grid(self):
        g = detect_grid(_make_grid(2, 3), 2400, 1080)
        self.assertEqual((g.n_rows, g.n_cols), (2, 3))
        self.assertEqual(len(g.cells), 6)

    def test_list_is_single_column(self):
        g = detect_grid(_make_grid(5, 1, w=520, h=120), 2400, 1080)
        self.assertIsNotNone(g)
        self.assertEqual(g.n_cols, 1)
        self.assertTrue(g.is_list)
        self.assertEqual(g.n_rows, 5)

    def test_noise_ignored(self):
        # a real 3x3 grid plus tiny icons and one big panel — only the grid wins
        els = _make_grid(3, 3)
        els += [_el(100, 100, 130, 130, label="icon")]          # tiny
        els += [_el(1900, 100, 2380, 1000, label="right panel")]  # huge
        g = detect_grid(els, 2400, 1080)
        self.assertEqual(len(g.cells), 9)

    def test_too_few_cells_returns_none(self):
        self.assertIsNone(detect_grid(_make_grid(1, 2), 2400, 1080, min_cells=4))

    def test_rel_region_locates_bottom_left(self):
        g = detect_grid(_make_grid(3, 3), 2400, 1080)
        cell = g.in_reading_order()[0]
        x0, y0, x1, y1 = cell.rel_region(0.0, 0.72, 0.36, 1.0)
        # bottom-left quadrant of the cell
        self.assertEqual(x0, cell.x1)
        self.assertGreater(y0, cell.cy)
        self.assertLess(x1, cell.cx + 1)


if __name__ == "__main__":
    unittest.main()
