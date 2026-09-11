"""Sell-down-to-N (partial-quantity sell) — trimming a stack to an exact keep level.

This primitive SELLS CARGO, so the tests are weighted toward the ways it must refuse:
a stack is only trimmed when the quantity dialog proved it opened AND the typed amount
was read back, and every abort must leave nothing sold and Put In Bulk restored."""
import types
import unittest

from actions.sell_goods import sell_down_to, _find_qty_field


_next_tile_y = [200]


def _good(name, owned, x=100, y=None):
    """Distinct tile coords per good — on the real sell page each good has its own tile,
    and the harness must be able to tell which one was tapped (the quantity dialog then
    shows THAT good's owned total)."""
    if y is None:
        _next_tile_y[0] += 120
        y = _next_tile_y[0]
    return types.SimpleNamespace(name=name, owned_qty=owned, tap_x=x, tap_y=y)


def _el(label, cx=1560, cy=708):
    return types.SimpleNamespace(label=label, cx=cx, cy=cy)


class _Harness:
    """Fake market: tracks taps, bulk state, and whether the qty dialog is 'open'."""

    # A real Trade Goods Info extent. The good's field lies inside it; the Cargo bar sits
    # in the right panel, outside — which is what tells the two apart.
    DIALOG_BBOX = (549, 107, 1855, 984)

    def __init__(self, goods, *, dialog_opens=True, keypad_ok=True, load_closes=True,
                 has_commit=True):
        self.goods = goods
        self.dialog_opens, self.keypad_ok = dialog_opens, keypad_ok
        self.load_closes, self.has_commit = load_closes, has_commit
        self.bulk = [True]
        self.typed, self.taps, self.dialog = [], [], False

    def capture(self):
        return object()

    def tap(self, x, y):
        self.taps.append((x, y))
        for g in self.goods:
            if g.tap_x == x and g.tap_y == y and self.dialog_opens:
                self.dialog = True
                self._dialog_owned = g.owned_qty     # the field shows THIS good's total
                break

    def omni(self, _frame):
        if not self.dialog:
            return [_el("Sell")]
        # The dialog shows "<staged> / <owned>" for the good whose tile was tapped, and the
        # Cargo bar ("3,823/4,108") is on screen too — same shape, different meaning.
        owned = self._dialog_owned if getattr(self, "_dialog_owned", None) else 1644
        return [_el("3,823 / 4,108", cx=2132, cy=213),      # Cargo bar: right panel
                _el(f"218 / {owned}")]                      # the good's field: in-dialog

    def read_page(self, _frame):
        return self.goods

    def set_bulk(self, target_on, _frame):
        self.bulk.append(target_on)
        return True

    def type_qty(self, qty, **_kw):
        if not self.keypad_ok:
            return False
        self.typed.append(qty)
        return True

    def overlay(self, _frame):
        """What the scrim says. A modal is up exactly while the dialog is open."""
        from vision.overlay import CLEAR, SCRIM, Overlay
        if self.dialog:
            return Overlay(kind="modal", state=SCRIM, bbox=self.DIALOG_BBOX)
        return Overlay(kind="none", state=CLEAR)

    def find_button(self, _frame, *_labels, **_kw):
        if self.load_closes:
            self.dialog = False          # Load closes the dialog
        return (1313, 943)

    def commit(self, _frame, _els):
        return types.SimpleNamespace(cx=900, cy=1000) if self.has_commit else None

    def react(self, _c, _t):
        return True

    def run(self, keep):
        return sell_down_to("Jakarta", keep, capture_fn=self.capture, tap_fn=self.tap,
                            omni_fn=self.omni, read_page_fn=self.read_page,
                            set_bulk_fn=self.set_bulk, type_qty_fn=self.type_qty,
                            commit_fn=self.commit, react_fn=self.react,
                            find_button_fn=self.find_button, overlay_fn=self.overlay,
                            # These tests are about TRIMMING; being on the Sell tab is a
                            # precondition with its own tests below.
                            ensure_sell_tab_fn=lambda *a, **k: True,
                            settle=0)


class FindQtyFieldTests(unittest.TestCase):
    """The field is found by WHERE it is — inside the dialog — not by matching a number
    we already believe. Requiring the denominator to equal the sell grid's badge let a
    grid misread veto the truth: live 2026-08-27 the grid read Candle 2148 (truly 148),
    so the real `1/148` field was refused and the trim aborted claiming the dialog had
    never opened, while it was plainly open on screen.
    """

    DIALOG = (549, 107, 1855, 984)          # a real Trade Goods Info extent

    def test_returns_the_position_and_the_owned_total(self):
        el = _el("744 / 1,644", cx=1560, cy=708)
        self.assertEqual(_find_qty_field([el], dialog_bbox=self.DIALOG),
                         (1560, 708, 1644))

    def test_the_denominator_alone_is_enough(self):
        """OmniParser reads the live `1/148` spinner as `/148` — the green slider splits
        the leading digit off. On the SELL dialog the cap IS the whole holding, so the
        denominator alone says we hold 148; the numerator is only what is dialled in."""
        el = _el("/148", cx=1529, cy=720)
        self.assertEqual(_find_qty_field([el], dialog_bbox=self.DIALOG),
                         (1529, 720, 148))

    def test_ignores_other_dialog_text(self):
        self.assertIsNone(_find_qty_field([_el("Load"), _el("Min"), _el("Sales Cost")],
                                          dialog_bbox=self.DIALOG))

    def test_the_cargo_bar_is_outside_the_dialog_and_ignored(self):
        """"N / M" is not unique on the sell page: the Cargo bar reads "3,040/4,952" and
        sits in the right panel, so a first-match scan finds IT. Tapping it opens Set Load
        Ratio, not the keypad — the live 2026-08-21 failure. It is OUTSIDE the dialog, so
        scoping excludes it without anyone having to predict the good's total."""
        cargo = _el("3,040 / 4,952", cx=2132, cy=213)      # right panel, outside
        field = _el("218 / 1,681", cx=900, cy=500)         # inside
        self.assertEqual(_find_qty_field([cargo, field], dialog_bbox=self.DIALOG),
                         (900, 500, 1681))

    def test_refuses_the_cargo_bar_when_the_good_field_is_absent(self):
        """Better to report not-found than to tap the wrong control."""
        cargo = _el("3,040 / 4,952", cx=2132, cy=213)
        self.assertIsNone(_find_qty_field([cargo], dialog_bbox=self.DIALOG))

    def test_no_dialog_means_no_search_at_all(self):
        """Unscoped, the first match on this page is the Cargo bar."""
        self.assertIsNone(_find_qty_field([_el("3,040 / 4,952", cx=2132, cy=213)],
                                          dialog_bbox=None))


class TrimTests(unittest.TestCase):
    def test_the_jakarta_case_trims_both_stacks_to_their_needs(self):
        h = _Harness([_good("Textiles", 1644), _good("Coral", 1238)])
        res = h.run({"Textiles": 900, "Coral": 1020})
        self.assertTrue(res["ok"])
        self.assertEqual(res["trimmed"], {"Textiles": 744, "Coral": 218})
        self.assertEqual(h.typed, [744, 218])       # sell the EXCESS, not the stack

    def test_bulk_goes_off_to_trim_and_is_restored_after(self):
        h = _Harness([_good("Textiles", 1644)])
        h.run({"Textiles": 900})
        self.assertIn(False, h.bulk)                 # turned off for the dialog
        self.assertTrue(h.bulk[-1])                  # handed back ON for the next buy

    def test_a_stack_already_at_or_below_the_keep_level_is_untouched(self):
        h = _Harness([_good("Coral", 1020)])
        res = h.run({"Coral": 1020})
        self.assertTrue(res["ok"])
        self.assertEqual(res["trimmed"], {})
        self.assertEqual(h.typed, [])
        self.assertTrue(h.bulk[-1])

    def test_goods_not_named_are_never_touched(self):
        h = _Harness([_good("Textiles", 1644), _good("Ruby", 50)])
        res = h.run({"Textiles": 900})
        self.assertEqual(list(res["trimmed"]), ["Textiles"])


class RefusalTests(unittest.TestCase):
    """Every one of these must sell NOTHING and restore bulk."""

    def _assert_clean_abort(self, h, res, needle):
        self.assertFalse(res["ok"])
        self.assertEqual(res["trimmed"], {})
        self.assertIn(needle, res["reason"])
        self.assertTrue(h.bulk[-1], "Put In Bulk must be restored even on abort")

    def test_no_quantity_dialog_means_bulk_was_still_on(self):
        # The dangerous case: a tile tap with bulk ON loads the WHOLE stack.
        h = _Harness([_good("Textiles", 1644)], dialog_opens=False)
        res = h.run({"Textiles": 900})
        self._assert_clean_abort(h, res, "quantity dialog did not open")
        self.assertNotIn((900, 1000), h.taps)        # Sell was never tapped

    def test_an_unconfirmed_typed_quantity_aborts(self):
        # The recorded gotcha: digits drop ('21' for '218'). Never commit on doubt.
        h = _Harness([_good("Textiles", 1644)], keypad_ok=False)
        res = h.run({"Textiles": 900})
        self._assert_clean_abort(h, res, "could not confirm the typed quantity")

    def test_a_dialog_still_open_after_Load_aborts(self):
        h = _Harness([_good("Textiles", 1644)], load_closes=False)
        res = h.run({"Textiles": 900})
        self._assert_clean_abort(h, res, "still open after Load")

    def test_a_missing_sell_button_commits_nothing(self):
        h = _Harness([_good("Textiles", 1644)], has_commit=False)
        res = h.run({"Textiles": 900})
        self._assert_clean_abort(h, res, "no Sell button")

    def test_an_unreadable_owned_count_is_skipped_not_guessed(self):
        h = _Harness([_good("Textiles", None)])
        res = h.run({"Textiles": 900})
        self.assertTrue(res["ok"])
        self.assertEqual(res["trimmed"], {})
        self.assertIn("owned quantity unreadable", res["skipped"][0])

    def test_a_good_missing_from_the_page_is_reported_not_assumed(self):
        h = _Harness([_good("Coral", 1238)])
        res = h.run({"Textiles": 900})
        self.assertEqual(res["trimmed"], {})
        self.assertIn("not on the sell page", res["skipped"][0])


if __name__ == "__main__":
    unittest.main()


class PostLoadCheckTests(unittest.TestCase):
    """After Load, "is the dialog still open?" must not be answered by the Cargo bar.

    Live 2026-08-22: immediately after "Keypad: 981 entered and confirmed", the trim
    aborted with "quantity dialog still open after Load". Load had in fact worked — the
    dialog had closed, revealing the sell page whose Cargo bar reads "3,823/4,108". That
    matches the same "N / M" shape, so the check saw it and concluded the dialog was up.

    The count that used to distinguish them can itself be misread (Candle 2148 for 148,
    live 2026-08-27), so the question is now answered by the SCRIM: it lifts when the
    dialog closes, and with no modal up there is nothing to search at all.
    """

    def test_the_cargo_bar_alone_does_not_mean_the_dialog_is_open(self):
        """The dialog closed, so no scrim, so no bbox — and no search happens."""
        els = [_el("3,823 / 4,108", cx=2132, cy=213)]
        self.assertIsNone(_find_qty_field(els, dialog_bbox=None))

    def test_the_dialog_is_still_detected_when_it_really_is_open(self):
        els = [_el("3,823 / 4,108", cx=2132, cy=213),
               _el("981 / 1,681", cx=900, cy=500)]
        self.assertEqual(_find_qty_field(els, dialog_bbox=_Harness.DIALOG_BBOX),
                         (900, 500, 1681))

    def test_a_successful_load_reports_the_trim(self):
        """End to end: dialog opens, quantity types, Load closes it -> trimmed recorded."""
        h = _Harness([_good("Ebony", 1681)])
        h.load_closes = True
        res = h.run({"Ebony": 700})
        self.assertTrue(res["ok"], res.get("reason"))
        self.assertEqual(res["trimmed"], {"Ebony": 981})


class ItMustBeLOOKINGAtTheHoldBeforeItSaysAnythingAboutIt(unittest.TestCase):
    """The trim read the PURCHASE page and called the hold trimmed.

    Live 2026-09-04 at Madeira (frame 277 of trace_barter_cmd_2026-09-04T17-35-01 — title
    "Purchase", the shop's stock in the middle, the fleet's 3,237 Pig in the panel on the
    right):

        [Madeira] nothing over-stocked — leaving the market untouched (hold {})
        trim skipped at Madeira: Raisin: not on the sell page
        trim skipped at Madeira: Pig: not on the sell page

    and the leg reported ok. The whole 1,476-unit Pig surplus then sailed to the village.

    TWO INDEPENDENT HOLES, both closed here. `sell_goods` and `_read_owned_via_sell` have
    always switched to the Sell tab and refused without it; `sell_down_to` alone never did.
    And `_sell_page` returns None for "I was not looking at the hold" against [] for "I was,
    and it holds nothing" — a distinction it was given on 2026-09-01 for exactly this failure
    — which `or []` then threw away one call later.
    """

    def _trim(self, *, on_sell, page):
        return sell_down_to("Madeira", {"Pig": 1260, "Raisin": 1260},
                            capture_fn=lambda: object(),
                            tap_fn=lambda *a: None,
                            read_page_fn=lambda _f: page,
                            set_bulk_fn=lambda *a, **k: True,
                            ensure_sell_tab_fn=lambda *a, **k: on_sell,
                            settle=0)

    def test_it_refuses_when_the_sell_tab_was_never_reached(self):
        res = self._trim(on_sell=False, page=[])
        self.assertFalse(res["ok"], "a Purchase-page read must not report the hold trimmed")
        self.assertIn("Sell grid", res["reason"])

    def test_an_UNREADABLE_page_is_not_an_empty_hold(self):
        """None from `_sell_page` means the grid was never read. `or []` made that
        indistinguishable from a hold with nothing over-stocked."""
        res = self._trim(on_sell=True, page=None)
        self.assertFalse(res["ok"])
        self.assertIn("could not be read", res["reason"])

    def test_it_says_WHICH_goods_it_could_not_speak_for(self):
        for page in ([], None):
            with self.subTest(page=page):
                res = self._trim(on_sell=(page is not None), page=page)
                skipped = " ".join(res.get("skipped") or ())
                self.assertIn("Pig", skipped)
                self.assertIn("Raisin", skipped)

    def test_a_GENUINELY_empty_hold_still_reads_as_nothing_to_trim(self):
        """[] means "I looked, and there is nothing" — that must stay a success, or every
        already-trimmed leg becomes a failure."""
        res = self._trim(on_sell=True, page=[])
        self.assertTrue(res["ok"])
        self.assertEqual(res["trimmed"], {})
