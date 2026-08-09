"""Action executor — op→primitive mapping + commit/spend safety gate (mocked)."""
import unittest

from brain.action_executor import execute, ExecResult, _is_commit_label, _resolve_element


def _mock_prims():
    calls = []
    prims = {
        "navigate_to_building": lambda name: calls.append(("nav", name)) or True,
        "exit_to_overworld":    lambda: calls.append(("exit",)) or True,
        "back":                 lambda: calls.append(("back",)),
        "sleep":                lambda: calls.append(("sleep",)),
        "tap_label":            lambda frame, label: calls.append(("tap", label)) or True,
        "tap_xy":               lambda x, y: calls.append(("tap_xy", x, y)) or True,
    }
    return prims, calls


# recruit-screen inventory: a right-panel action BUTTON and a same-text top-bar TITLE
_RECRUIT_ELEMENTS = [
    {"id": "e1", "label": "Recruit",      "type": "button", "region": "RIGHT-PANEL", "cx": 2076, "cy": 940, "conf": 0.62},
    {"id": "e2", "label": "Recruit Crew", "type": "button", "region": "TOP-BAR",     "cx": 269,  "cy": 59,  "conf": 0.55},
    {"id": "e3", "label": "Supply",       "type": "button", "region": "LEFT-MENU",   "cx": 252,  "cy": 165, "conf": 0.5},
]


class OpMappingTests(unittest.TestCase):
    def test_go_to_building(self):
        prims, calls = _mock_prims()
        r = execute({"op": "go_to_building", "arg": "Harbor"}, prims=prims)
        self.assertTrue(r.ok)
        self.assertIn(("nav", "Harbor"), calls)

    def test_go_to_building_missing_arg(self):
        prims, calls = _mock_prims()
        r = execute({"op": "go_to_building", "arg": None}, prims=prims)
        self.assertFalse(r.ok)
        self.assertEqual(calls, [])

    def test_exit_back_wait(self):
        prims, calls = _mock_prims()
        self.assertTrue(execute({"op": "exit_building"}, prims=prims).ok)
        self.assertTrue(execute({"op": "back"}, prims=prims).ok)
        self.assertTrue(execute({"op": "wait"}, prims=prims).ok)
        self.assertEqual(calls, [("exit",), ("back",), ("sleep",)])

    def test_tap_navigation_label(self):
        prims, calls = _mock_prims()
        r = execute({"op": "tap", "arg": "Recruit"}, frame=object(), prims=prims)
        self.assertTrue(r.ok)
        self.assertIn(("tap", "Recruit"), calls)

    def test_abort_does_nothing(self):
        prims, calls = _mock_prims()
        r = execute({"op": "abort", "why": "no crew available"}, prims=prims)
        self.assertFalse(r.ok)
        self.assertEqual(calls, [])

    def test_unknown_op(self):
        prims, _ = _mock_prims()
        self.assertFalse(execute({"op": "teleport"}, prims=prims).ok)


class ElementResolutionTests(unittest.TestCase):
    def test_tap_resolves_to_right_panel_button_not_title(self):
        # 'Recruit' must hit the RIGHT-PANEL button @(2076,940), not the top-bar title
        t = _resolve_element("Recruit", _RECRUIT_ELEMENTS)
        self.assertEqual((t["cx"], t["cy"]), (2076, 940))
        self.assertEqual(t["region"], "RIGHT-PANEL")

    def test_execute_tap_uses_element_coords(self):
        prims, calls = _mock_prims()
        r = execute({"op": "tap", "arg": "Recruit"}, frame=object(),
                    elements=_RECRUIT_ELEMENTS, prims=prims)
        self.assertTrue(r.ok)
        self.assertIn(("tap_xy", 2076, 940), calls)   # tapped coords, not text-search
        self.assertNotIn(("tap", "Recruit"), calls)

    def test_tap_falls_back_to_text_search_without_inventory(self):
        prims, calls = _mock_prims()
        r = execute({"op": "tap", "arg": "Recruit"}, frame=object(),
                    elements=None, prims=prims)
        self.assertTrue(r.ok)
        self.assertIn(("tap", "Recruit"), calls)      # legacy path preserved

    def test_tap_by_element_id(self):
        prims, calls = _mock_prims()
        execute({"op": "tap", "arg": "e3"}, frame=object(),
                elements=_RECRUIT_ELEMENTS, prims=prims)
        self.assertIn(("tap_xy", 252, 165), calls)

    def test_commit_entry_preferred_and_tag_tolerated(self):
        # a yellow [COMMIT] entry coexists with a same-region toggle button
        inv = [
            {"id": "e1", "label": "Normal Recruit", "type": "button", "region": "RIGHT-PANEL", "cx": 2078, "cy": 869, "conf": 0.5},
            {"id": "e2", "label": "Recruit", "type": "commit", "region": "RIGHT-PANEL", "cx": 2076, "cy": 940, "cost": "205,848", "conf": 0.38},
        ]
        self.assertEqual(_resolve_element("Recruit", inv)["cx"], 2076)          # commit
        self.assertEqual(_resolve_element("[COMMIT] Recruit", inv)["cx"], 2076)  # tag tolerated
        prims, calls = _mock_prims()
        execute({"op": "tap", "arg": "Recruit"}, frame=object(), elements=inv, prims=prims)
        self.assertIn(("tap_xy", 2076, 940), calls)


class SafetyGateTests(unittest.TestCase):
    def test_commit_tap_refused_by_default(self):
        prims, calls = _mock_prims()
        r = execute({"op": "tap", "arg": "Confirm Purchase"}, frame=object(), prims=prims)
        self.assertFalse(r.ok)
        self.assertTrue(r.refused)
        self.assertEqual(calls, [])          # never tapped

    def test_commit_tap_allowed_with_confirmation(self):
        prims, calls = _mock_prims()
        r = execute({"op": "tap", "arg": "Confirm"}, frame=object(), prims=prims,
                    confirm_fn=lambda label: True)
        self.assertTrue(r.ok)
        self.assertIn(("tap", "Confirm"), calls)

    def test_navigation_labels_not_flagged(self):
        # 'Purchase' tab / 'Recruit' / 'Supply' are navigation, not commits
        for lbl in ("Purchase", "Recruit", "Supply", "Sell", "Harbor"):
            self.assertFalse(_is_commit_label(lbl), lbl)
        for lbl in ("Confirm", "Confirm Purchase", "Pay", "Buy Now"):
            self.assertTrue(_is_commit_label(lbl), lbl)


if __name__ == "__main__":
    unittest.main()
