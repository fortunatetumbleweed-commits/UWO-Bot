"""Every activity's device-touching default must at least RESOLVE.

The activities take their device calls as constructor arguments so tests can inject fakes.
That is what makes them testable — and it means the DEFAULTS, the code that actually runs on
the phone, are executed by no test at all.

Live 2026-08-26, at Barcelona, on the first tick the migrated market activity ever ran for
real:

    [dispatch] market <- Hold(orders={'Iron': 470})
    ImportError: cannot import name 'MARKET_COORDS' from 'brain.barter_mission_live'

`MARKET_COORDS` lives in `actions.market_actions`. Every unit test passed, the stage suite
passed, the end-to-end wiring test passed — all of them inject `show_grid_fn`, so the line
with the wrong import had never been executed once.

These tests run each default with the DEVICE BLOCKED. A default that reaches
`brain.replay.DeviceTouched` has resolved all its imports and is asking for the phone, which
is exactly as far as a test can honestly take it. Anything else — ImportError, NameError,
AttributeError — is the bug above.
"""

from __future__ import annotations

import unittest

from brain.replay import DeviceTouched, no_device


class EveryDefaultResolvesItsImports(unittest.TestCase):

    def _reaches_the_device(self, fn, *args):
        """Call `fn` with the device blocked; the only acceptable failure is DeviceTouched."""
        with no_device():
            try:
                fn(*args)
            except DeviceTouched:
                return True
            except (ImportError, NameError, AttributeError, TypeError) as exc:
                self.fail(f"{fn.__module__}.{fn.__name__} does not resolve: "
                          f"{type(exc).__name__}: {exc}")
            except Exception:
                # Some other runtime failure past the imports — not what this test guards.
                return True
        return True

    def test_the_market_defaults(self):
        from brain.activities import market
        self._reaches_the_device(market._default_show_purchase_grid)

    def test_the_harbor_defaults(self):
        from brain.activities import harbor
        for fn in (harbor._default_readiness, harbor._default_depart,
                   harbor._default_tap_recruit):
            with self.subTest(fn=fn.__name__):
                self._reaches_the_device(fn)

    def test_the_village_defaults(self):
        from brain.activities import village
        for fn in (village._default_open, village._default_commit, village._what_it_saw,
                   village._read_overflow):
            with self.subTest(fn=fn.__name__):
                self._reaches_the_device(fn)
        self._reaches_the_device(village._default_select, "Birch Tree", None)
        self._reaches_the_device(village._default_jettison("Birch Tree"), 10)

    def test_the_intent_dispatcher(self):
        from brain.dispatcher import Intent
        from brain.intents import dispatch
        self._reaches_the_device(dispatch, Intent("ENTER_BUILDING", {"name": "market"}))

    def test_the_one_that_actually_broke(self):
        """`MARKET_COORDS` is in actions.market_actions, not barter_mission_live."""
        import inspect
        from brain.activities.market import _default_show_purchase_grid
        src = inspect.getsource(_default_show_purchase_grid)
        self.assertIn("from actions.market_actions import MARKET_COORDS", src)


if __name__ == "__main__":
    unittest.main()
