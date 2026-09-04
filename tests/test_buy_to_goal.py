"""Goal-driven gather with blue-gem refresh: progress by RELIABLE cargo-total delta,
refresh only on genuine sold-out. All deps mocked — no device.
Regression for 2026-08-17: a single bulk buy overshooting the goal must NOT refresh."""
from dataclasses import dataclass

from actions.buy_materials import buy_to_goal


@dataclass
class _Good:
    name: str
    available_qty: int
    sold_out: bool = False


class _Sim:
    """A market with restockable shelves. Each 'level' is one shelf; a bulk buy takes
    the whole shelf (→ sold_out) and adds it to cargo; refresh advances to the next."""
    def __init__(self, shelves, cargo_start=0, sells_out=True):
        self.shelves = shelves          # list[{good: qty}] per restock level
        self.level = 0
        self.cargo = cargo_start
        self._bought = False            # bought the current shelf?
        self.blocked = False            # a popup / greeting page covering the grid
        self.sells_out = sells_out      # False = stays available at full qty (Malé Coral)
        self.log = {"buys": [], "refreshes": 0}

    def _shelf(self):
        return self.shelves[min(self.level, len(self.shelves) - 1)]

    def read_market(self, frame, tab="purchase", port="", elements=None):
        if self.blocked:
            return []                   # grid not perceivable until the blocker clears
        if not self.sells_out:          # good never depletes — always full shelf, never sold out
            return [_Good(g, q, sold_out=False) for g, q in self._shelf().items()]
        return [_Good(g, 0 if self._bought else q, sold_out=self._bought)
                for g, q in self._shelf().items()]

    def cargo_of(self, frame):
        return self.cargo

    def buy(self, port, goods, **kw):
        for g in goods:
            self.cargo += self._shelf().get(g, 0)
        if self.sells_out:
            self._bought = True
        self.log["buys"].append(list(goods))
        return {"ok": True}

    def refresh(self, capture_fn=None, tap_fn=None, **kw):
        self.log["refreshes"] += 1
        self.log.setdefault("verify_goods", []).append(kw.get("verify_good"))
        self.level += 1
        self._bought = False
        return {"ok": True}


def _run(goal, sim, max_rounds=6, refresh_ok=True):
    refresh = sim.refresh if refresh_ok else (lambda **kw: {"ok": False})
    res = buy_to_goal("P", goal, max_rounds=max_rounds,
                      capture_fn=lambda: None, tap_fn=lambda *a: None,
                      omni_fn=lambda f: [], read_market_fn=sim.read_market,
                      buy_round_fn=sim.buy, refresh_fn=refresh, cargo_fn=sim.cargo_of)
    return res, sim.log


def test_bulk_buy_overshoot_does_NOT_refresh():
    # THE regression: goal 180, one shelf of 552 → met on first buy → no refresh.
    res, log = _run({"Textiles": 180}, _Sim([{"Textiles": 552}]))
    assert res["met"] is True
    assert res["bought_total"] == 552
    assert log["refreshes"] == 0                 # ← the fix
    assert log["buys"] == [["Textiles"]]


def test_meets_goal_with_one_refresh_when_shelf_short():
    res, log = _run({"Coral": 500}, _Sim([{"Coral": 200}, {"Coral": 400}]))
    assert res["met"] and res["bought_total"] == 600
    assert log["refreshes"] == 1


def test_no_refresh_when_first_shelf_is_enough():
    res, log = _run({"Coral": 100}, _Sim([{"Coral": 173}]))
    assert res["met"] and res["bought_total"] == 173 and log["refreshes"] == 0


def test_stops_when_refresh_unavailable_returns_partial():
    res, log = _run({"Coral": 1000}, _Sim([{"Coral": 200}]), refresh_ok=False)
    assert res["met"] is False and res["ok"] is True and res["bought_total"] == 200


def test_available_good_never_sells_out_keeps_buying():
    # THE Malé Coral case (live 2026-08-18): buying doesn't sell the shelf out (it stays
    # available at a rising price), so the loop must KEEP BUYING to reach the goal — not
    # stop after one shelf, and not refresh.
    sim = _Sim([{"Coral": 173}], sells_out=False)
    res, log = _run({"Coral": 250}, sim)
    assert res["met"] and res["bought_total"] >= 250
    assert log["refreshes"] == 0 and len(log["buys"]) == 2


def test_grayed_tile_triggers_refresh_even_when_reader_misses_soldout():
    # THE 2026-08-18 fix: the reader misses the Sold-Out stamp (sold_out stays False), but
    # the tile GRAYS OUT after the buy → that grayed signal must trigger the refresh.
    sim = _Sim([{"Coral": 200}, {"Coral": 400}])

    def read_no_soldout(frame, tab="purchase", port="", elements=None):
        # reader NEVER reports sold_out (the bug); qty goes 0 after buying the shelf
        return [_Good(g, 0 if sim._bought else q, sold_out=False)
                for g, q in sim._shelf().items()]

    grayed = {"n": 0}

    def tile_grayed(before, after, xy):
        # tile is grayed iff the shelf was just bought out (sim._bought True at re-read)
        if sim._bought:
            grayed["n"] += 1
            return True
        return False

    res = buy_to_goal("P", {"Coral": 500}, max_rounds=6,
                      capture_fn=lambda: object(), tap_fn=lambda *a: None, omni_fn=lambda f: ["e"],
                      read_market_fn=read_no_soldout, buy_round_fn=sim.buy,
                      refresh_fn=sim.refresh, cargo_fn=sim.cargo_of,
                      clear_blockers_fn=lambda f: {"cleared": False}, show_grid_fn=lambda: None,
                      tile_grayed_fn=tile_grayed, find_tile_fn=lambda els, m: (689, 554))
    assert res["met"] and res["bought_total"] == 600
    assert sim.log["refreshes"] == 1 and grayed["n"] >= 1


def test_buy_noop_triggers_refresh():
    # Reader misses sold-out AND there's no colour transition (already-empty shelf) → the
    # got==0 backstop must still trigger a refresh, not a silent stop.
    state = {"stock": 0, "cargo": 0, "refreshed": False}

    def read(frame, tab="purchase", port="", elements=None):
        return [_Good("Coral", state["stock"], sold_out=False)]

    def buy(port, goods, **kw):
        state["cargo"] += state["stock"]        # no-op while stock is 0
        return {"ok": state["stock"] > 0}

    def refresh(**kw):
        state["stock"] = 100                    # restock
        state["refreshed"] = True
        return {"ok": True}

    res = buy_to_goal("P", {"Coral": 50}, max_rounds=6,
                      capture_fn=lambda: None, tap_fn=lambda *a: None, omni_fn=lambda f: [],
                      read_market_fn=read, buy_round_fn=buy, refresh_fn=refresh,
                      cargo_fn=lambda f: state["cargo"],
                      clear_blockers_fn=lambda f: {"cleared": False}, show_grid_fn=lambda: None,
                      tile_grayed_fn=lambda b, a, xy: False)
    assert res["met"] and state["refreshed"] and res["bought_total"] == 100


def test_bounded_by_max_rounds():
    # Each round does ONE action (buy or refresh); max_rounds caps the total. With the
    # last-round refresh guard: r0 buy, r1 refresh, r2 buy, r3 would-refresh→stop.
    res, log = _run({"Coral": 10_000}, _Sim([{"Coral": 100}] * 5), max_rounds=4)
    assert log["refreshes"] == 1 and len(log["buys"]) == 2 and res["met"] is False


def test_multi_material_goal():
    res, log = _run({"Coral": 150, "Ebony": 150},
                    _Sim([{"Coral": 100, "Ebony": 100}] * 2))
    assert res["met"] and res["bought_total"] == 400 and log["refreshes"] == 1


def test_empty_grid_clears_blocker_then_buys():
    # THE 2026-08-18 regression: a promo popup covered the grid → first read empty.
    # buy_to_goal must clear the blocker + re-read, NOT give up with bought=0.
    sim = _Sim([{"Coral": 100}])
    sim.blocked = True
    seen = {"cleared": 0}

    def clear(frame):
        sim.blocked = False           # dismissing the popup reveals the grid
        seen["cleared"] += 1
        return {"cleared": True}

    res = buy_to_goal("P", {"Coral": 50}, max_rounds=3,
                      capture_fn=lambda: None, tap_fn=lambda *a: None,
                      omni_fn=lambda f: [], read_market_fn=sim.read_market,
                      buy_round_fn=sim.buy, refresh_fn=sim.refresh, cargo_fn=sim.cargo_of,
                      clear_blockers_fn=clear, show_grid_fn=lambda: None)
    assert res["met"] and res["bought_total"] == 100
    assert seen["cleared"] == 1 and any(r.get("cleared_blocker") for r in res["rounds"])


def test_persistent_empty_grid_gives_up_gracefully():
    # Grid never readable (nothing to clear) → bounded retries, no crash, honest failure.
    sim = _Sim([{"Coral": 100}])
    sim.blocked = True
    res = buy_to_goal("P", {"Coral": 50}, max_rounds=3,
                      capture_fn=lambda: None, tap_fn=lambda *a: None,
                      omni_fn=lambda f: [], read_market_fn=sim.read_market,
                      buy_round_fn=sim.buy, refresh_fn=sim.refresh, cargo_fn=sim.cargo_of,
                      clear_blockers_fn=lambda f: {"cleared": False}, show_grid_fn=lambda: None)
    assert res["met"] is False and res["bought_total"] == 0


def test_refresh_is_told_which_sold_out_good_to_verify():
    # buy 500 Coral from 200-unit shelves → each refresh must name the sold-out good
    # so refresh_market can verify by that tile going active again.
    res, log = _run({"Coral": 500}, _Sim([{"Coral": 200}, {"Coral": 400}]))
    assert res["met"] and log["verify_goods"] == ["Coral"]


def test_it_stops_the_first_time_the_owned_count_is_unreadable():
    """NEVER BUY BLIND.

    If the owned count cannot be read, the loop cannot tell whether it is making progress —
    and it keeps SPENDING while it fails to find out. Live twice: 1,681 Ebony bought against
    a goal of 350 (2026-08-21), and at Bordeaux on 2026-08-24 four Purchase taps at 149,695
    ducats each while the counter sat at 0/777, on course for all 39 rounds.

    One buy is defensible — the read might recover. A second is not: nothing has changed
    that would make it readable, so the loop must stop and report (user, 2026-08-24).
    """
    sim = _Sim([{"Coral": 200}] * 40, sells_out=False)
    sim.cargo_of = lambda _frame: None                  # the cargo strip cannot be read
    res, log = _run({"Coral": 777}, sim, max_rounds=39)

    assert res["met"] is False
    assert res["ok"] is False, "a blind loop is a failure to report, not a partial success"
    assert len(log["buys"]) == 1, f"bought {len(log['buys'])} times while blind"
    assert "unreadable" in res["reason"].lower()


def test_a_readable_count_still_runs_to_the_goal():
    """The blind-stop must not fire on a loop that CAN see itself progressing."""
    sim = _Sim([{"Coral": 100}] * 40, sells_out=False)
    res, log = _run({"Coral": 350}, sim, max_rounds=39)
    assert res["met"] is True and res["bought_total"] >= 350
    assert len(log["buys"]) >= 4
