"""The Enter-Number keypad is read as a GRID, relative to the dialog's own bounds.

Identity comes from where a key sits in the block, not from OCR-ing its glyph:

    row0:  1   2   3   <backspace>
    row1:  4   5   6   <enter, tall: spans rows 1-3>
    row2:  7   8   9
    row3:  0   [ Max spans two columns ]

The previous detector ran EasyOCR with a digit allowlist over a crop `x > x_min` and
filtered by glyph size. It failed both ways on the live SELL trim (2026-08-22): it matched
stray numerals from the price chart underneath the dialog, and missed real keys — aborting
on "digit '8' not detected", then "digit '9' not detected", so the trim never ran and the
hold stayed at 3823/4108. On the captured frame it returned {'0': (1366,186),
'5': (1867,830)} — neither point inside the keypad.

Columns are derived from the DIALOG WIDTH rather than by clustering the detections: stray
sub-elements (measured at cx 1091 and 1313) bridged neighbouring columns and collapsed
three digit columns into two, so "3" resolved to "2"'s position.

User, 2026-08-22: "instead of using absolute positions, please get the dialog bbox, and use
relative positions."
"""
from actions.market_actions import detect_keypad_grid


class _El:
    def __init__(self, x1, y1, x2, y2, label="icon", element_type="icon"):
        self.x1, self.y1, self.x2, self.y2 = x1, y1, x2, y2
        self.label, self.element_type = label, element_type

    @property
    def cx(self): return (self.x1 + self.x2) // 2

    @property
    def cy(self): return (self.y1 + self.y2) // 2


def _live_keypad():
    """Transcribed from frame_0021 of trace_barter_cmd_2026-08-22T17-58-13."""
    return [
        _El(1093, 239, 1311, 275, "Enter Number", "text"),
        # The number display, with the value OmniParser already read off it.
        _El(955, 296, 1444, 375, "981", "button"),
        _El(956, 385, 1076, 493), _El(1078, 385, 1199, 492),
        _El(1201, 385, 1321, 491), _El(1324, 385, 1443, 491),   # 1 2 3 <-
        _El(956, 500, 1075, 605), _El(1079, 500, 1199, 605),
        _El(1201, 500, 1322, 605),                              # 4 5 6
        _El(1324, 499, 1444, 837),                              # enter (tall)
        _El(955, 612, 1076, 720), _El(1078, 612, 1198, 721),
        _El(1200, 612, 1323, 721),                              # 7 8 9
        _El(957, 727, 1075, 834),                               # 0
        _El(1080, 726, 1319, 835, "Max", "button"),
        # stray sub-elements that used to bridge the columns
        _El(1091, 726, 1211, 833), _El(1313, 726, 1433, 833),
    ]


class TestGridMapping:
    def setup_method(self):
        self.g = detect_keypad_grid(None, _live_keypad())

    def test_every_key_is_found(self):
        for k in list("0123456789") + ["enter", "backspace", "max"]:
            assert k in self.g, f"{k!r} missing from the detected keypad"

    def test_digits_are_in_the_right_grid_positions(self):
        """Same row => same y, same column => same x, within a few px (the real bboxes
        differ by a pixel or two, e.g. 1015 vs 1016)."""
        g = self.g
        TOL = 6

        def aligned(vals):
            return max(vals) - min(vals) <= TOL

        for row in (("1", "2", "3"), ("4", "5", "6"), ("7", "8", "9")):
            assert aligned([g[d][1] for d in row]), f"row {row} not aligned"
        for col in (("1", "4", "7", "0"), ("2", "5", "8"), ("3", "6", "9")):
            assert aligned([g[d][0] for d in col]), f"column {col} not aligned"

    def test_columns_are_distinct(self):
        """The clustering bug collapsed columns 2 and 3, so '3' typed a '2'."""
        xs = sorted(self.g[d][0] for d in ("1", "2", "3"))
        assert xs[1] - xs[0] > 50 and xs[2] - xs[1] > 50, \
            f"digit columns collapsed: {xs}"

    def test_rows_descend(self):
        assert self.g["1"][1] < self.g["4"][1] < self.g["7"][1] < self.g["0"][1]

    def test_enter_is_the_tall_key_on_the_right(self):
        assert self.g["enter"][0] > self.g["3"][0]
        assert self.g["enter"][1] > self.g["backspace"][1]

    def test_max_is_not_mistaken_for_a_digit(self):
        assert self.g["max"] not in [self.g[d] for d in "0123456789"]


class TestFailsClosed:
    def test_no_keypad_yields_no_grid(self):
        assert detect_keypad_grid(None, [_El(0, 0, 100, 50, "Sell", "text")]) == {}

    def test_a_title_without_a_keypad_yields_no_grid(self):
        els = [_El(1093, 239, 1311, 275, "Enter Number", "text")]
        assert detect_keypad_grid(None, els) == {}

    def test_no_elements_at_all(self):
        assert detect_keypad_grid(None, []) == {}


class TestDisplayReadback:
    """The typed value is read from the display's OWN bbox.

    Deriving a band from the digit geometry swept in the "Enter Number" title and the
    dialog underneath, and every digit character found was concatenated: on the live frame
    that produced 1146198 (and 11461 on the captured one) while the field showed 1. The
    typed value then never matched, so the routine cleared and retyped until it gave up.
    """

    def test_the_grid_exposes_the_display_bbox(self):
        g = detect_keypad_grid(None, _live_keypad())
        assert g["display_box"] == (955, 296, 1444, 375)

    def test_the_display_box_excludes_the_title(self):
        """The title sits above the display; including it corrupted the readback."""
        g = detect_keypad_grid(None, _live_keypad())
        title_bottom = 275
        assert g["display_box"][1] >= title_bottom

    def test_the_display_box_excludes_the_keys(self):
        g = detect_keypad_grid(None, _live_keypad())
        assert g["display_box"][3] <= g["1"][1]


class TestTypingLoop:
    """Clearing must react to what is on screen, and Enter is pressed only on a match."""

    def _type(self, screen_values, want=981):
        """Drive type_quantity_on_keypad with a scripted sequence of display readings."""
        from unittest import mock
        from actions import market_actions as ma
        seq = list(screen_values)
        taps = []

        def fake_read(_frame, _grid=None):
            return seq.pop(0) if seq else want

        grid = detect_keypad_grid(None, _live_keypad())
        with mock.patch.object(ma, "detect_keypad_grid", return_value=grid), \
             mock.patch.object(ma, "keypad_display_value", side_effect=fake_read), \
             mock.patch("time.sleep"), \
             mock.patch("random.uniform", return_value=0):
            ok = ma.type_quantity_on_keypad(
                want, capture_fn=lambda: object(),
                tap_fn=lambda x, y: taps.append((x, y)))
        return ok, taps, grid

    def test_an_empty_field_is_not_backspaced(self):
        """user 2026-08-22: it 'kept tapping the back arrow key when it was already 0'."""
        ok, taps, grid = self._type([None])
        assert grid["backspace"] not in taps, "backspaced an already-empty field"

    def test_enter_is_pressed_once_the_value_matches(self):
        """user 2026-08-22: 'should be tapping the return button when the desired number
        is in it'."""
        ok, taps, grid = self._type([None])
        assert ok is True
        assert taps[-1] == grid["enter"], f"last tap was {taps[-1]}, not Enter"

    def test_the_digits_typed_are_the_requested_ones(self):
        _ok, taps, grid = self._type([None])
        typed = [t for t in taps if t in [grid[d] for d in "0123456789"]]
        assert typed == [grid["9"], grid["8"], grid["1"]]


class TestDisplayValueIsRelativeToTheElement:
    """The displayed number comes from the display ELEMENT — its label, else its own bbox.

    Live 2026-08-22, frame_0049: the old band ran to x = max(key)+140 = 1401 while the
    display extends to 1444, so the trailing digit sat OUTSIDE the crop. It read '98',
    concatenated digits from the "Enter Number" title above, and returned 11461 — with 981
    plainly on screen. The value never matched, so the routine cleared and retyped. The
    trace shows the cost: eight consecutive backspaces at (1384,438), then 9-8-1 retyped,
    then abort.
    """

    def test_the_label_is_used_when_omniparser_read_it(self):
        from actions.market_actions import keypad_display_value
        g = detect_keypad_grid(None, _live_keypad())
        assert keypad_display_value(None, g) == 981

    def test_an_empty_field_reads_as_none_not_a_number(self):
        """When the field is empty OmniParser labels it 'icon' — that must not become a
        number, or the clearing loop backspaces against a phantom value."""
        from actions.market_actions import keypad_display_value
        els = [e for e in _live_keypad()]
        for e in els:
            if (e.x1, e.y1, e.x2, e.y2) == (955, 296, 1444, 375):
                e.label = "icon"
        g = detect_keypad_grid(None, els)
        assert keypad_display_value(None, g) is None

    def test_the_display_box_is_wide_enough_for_every_digit(self):
        """The old crop stopped 43px short of the display's right edge."""
        g = detect_keypad_grid(None, _live_keypad())
        box = g["display_box"]
        keys_right = max(g[d][0] for d in "0123456789")
        assert box[2] > keys_right + 140, (
            "the display extends past keys+140 — a padded band truncates the last digit")
