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
        return [_el("3,823 / 4,108"), _el(f"218 / {owned}")]

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
                            find_button_fn=self.find_button, settle=0)


class FindQtyFieldTests(unittest.TestCase):
    def test_matches_the_n_over_owned_field(self):
        self.assertEqual(_find_qty_field([_el("744 / 1,644")]), (1560, 708))

    def test_ignores_other_dialog_text(self):
        self.assertIsNone(_find_qty_field([_el("Load"), _el("Min"), _el("Sales Cost")]))

    def test_prefers_the_field_whose_total_is_what_we_own(self):
        """"N / M" is not unique on the sell page: the Cargo bar reads "3,823/4,108" and
        sits ABOVE the goods, so a first-match scan finds IT. Tapping the Cargo bar opens
        Set Load Ratio, not the keypad — the live 2026-08-21 failure."""
        cargo = _el("3,823 / 4,108")
        field = _el("218 / 1,681", cx=900, cy=500)
        self.assertEqual(_find_qty_field([cargo, field], expect_total=1681), (900, 500))

    def test_refuses_the_cargo_bar_when_the_good_field_is_absent(self):
        """Better to report not-found than to tap the wrong control."""
        self.assertIsNone(_find_qty_field([_el("3,823 / 4,108")], expect_total=1681))


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

    While the dialog IS open it shows "<staged> / <owned>", so the owned count is what
    distinguishes them. Load stages goods into the basket; it does not change what we own.
    """

    def test_the_cargo_bar_alone_does_not_mean_the_dialog_is_open(self):
        els = [_el("3,823 / 4,108")]
        self.assertIsNone(_find_qty_field(els, expect_total=1681))

    def test_the_dialog_is_still_detected_when_it_really_is_open(self):
        els = [_el("3,823 / 4,108"), _el("981 / 1,681", cx=900, cy=500)]
        self.assertEqual(_find_qty_field(els, expect_total=1681), (900, 500))

    def test_a_successful_load_reports_the_trim(self):
        """End to end: dialog opens, quantity types, Load closes it -> trimmed recorded."""
        h = _Harness([_good("Ebony", 1681)])
        h.load_closes = True
        res = h.run({"Ebony": 700})
        self.assertTrue(res["ok"], res.get("reason"))
        self.assertEqual(res["trimmed"], {"Ebony": 981})
