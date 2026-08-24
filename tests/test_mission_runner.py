"""EXECUTE-layer plumbing: sub-task graph + dynamic cost scheduler + recovery.
All executors are mocked — no device. See docs/opportunity_driven_architecture.md."""
from dataclasses import dataclass

from brain.mission import (
    Opportunity, default_opportunity, SubTask, build_barter_graph, MissionTail,
    make_barter_recover, run_mission, RecoveryDecision,
)
from memory.barter_kb import BarterRecipe, RecipeInput


# coords: a little map. distances chosen so ordering is unambiguous.
COORDS = {
    "Home":   (0, 0),
    "Near":   (1, 0),
    "Mid":    (5, 0),
    "Far":    (9, 0),
    "Village": (10, 0),
    "Jakarta": (12, 0),
    "Alt":    (2, 0),
}


@dataclass
class _Plan:
    purchases: dict


def _always_ok(_task):
    return {"ok": True}


# ── DECIDE default ────────────────────────────────────────────────────────────

def test_default_opportunity_is_barter_to_jakarta_timeless():
    opp = default_opportunity()
    assert opp.kind == "seasonal_barter"
    assert opp.sell_port == "Jakarta"
    assert opp.village == "Melanesian Village"
    assert opp.window is None            # time doesn't matter yet
    assert opp.reachable is True


# ── Graph shape / dependencies ────────────────────────────────────────────────

def _nutmeg_opp(**kw):
    kw = {"kind": "seasonal_barter", "good": "Box of Nutmeg", "sell_port": "Jakarta",
          "village": "Village", "rounds": 6, **kw}
    return Opportunity(**kw)


def test_build_barter_graph_deps():
    plan = _Plan(purchases={"Near": {"Coral": 100}, "Far": {"Ebony": 50}})
    g = {t.id: t for t in build_barter_graph(_nutmeg_opp(), plan)}
    assert g["gather:Near"].deps == () and g["gather:Far"].deps == ()
    # Trim surplus once every gather is in, THEN check supply, THEN sail: the trim
    # changes what is aboard, so a supply/space check before it would read stale.
    assert set(g["sell_surplus"].deps) == {"gather:Near", "gather:Far"}
    # supply_verify gates the village leg — a village cannot resupply (2026-08-20 loss).
    assert g["supply_verify"].deps == ("sell_surplus",)
    assert g["sail_to_village"].deps == ("supply_verify",)
    assert g["barter"].deps == ("sail_to_village",)
    assert g["sail_to_sell"].deps == ("barter",)
    assert g["sell"].deps == ("sail_to_sell",)


def test_the_trim_targets_come_from_the_plan_not_the_recipe():
    plan = _Plan(purchases={"Near": {"Coral": 1}})
    plan.needs = {"Coral": 900, "Ebony": 760}
    g = {t.id: t for t in build_barter_graph(_nutmeg_opp(), plan)}
    assert g["sell_surplus"].params["keep_qty"] == {"Coral": 900, "Ebony": 760}
    assert g["sell_surplus"].location == ""          # no travel; runs at the last gather


def test_supply_verify_does_not_move_the_fleet():
    # location "" is absent from the coord table → the scheduler charges no travel and
    # the position is unchanged, so the next leg still starts from the last gather port.
    plan = _Plan(purchases={"Near": {"Coral": 1}})
    g = {t.id: t for t in build_barter_graph(_nutmeg_opp(), plan)}
    assert g["supply_verify"].location == ""
    assert g["supply_verify"].location not in COORDS


def test_route_tail_replaces_the_free_sail_leg_and_defers_the_sell_port():
    plan = _Plan(purchases={"Near": {"Coral": 1}})
    tail = MissionTail(kind="route", value="jakarta to london")
    g = {t.id: t for t in build_barter_graph(_nutmeg_opp(), plan, tail=tail)}
    assert "sail_to_sell" not in g
    assert g["sail_route"].params["route"] == "jakarta to london"
    assert g["sail_route"].deps == ("barter",)
    assert g["sell"].deps == ("sail_route",)
    # A route ends where it ends — the sell node resolves the port on arrival.
    assert g["sell"].params["sell_port"] is None


def test_sail_tail_names_the_sell_port():
    plan = _Plan(purchases={"Near": {"Coral": 1}})
    tail = MissionTail(kind="sail", value="Edinburgh", sell_port="Edinburgh")
    g = {t.id: t for t in build_barter_graph(_nutmeg_opp(), plan, tail=tail)}
    assert g["sail_to_sell"].params["sell_port"] == "Edinburgh"
    assert g["sell"].params["sell_port"] == "Edinburgh"


def test_none_tail_ends_at_the_village():
    plan = _Plan(purchases={"Near": {"Coral": 1}})
    ids = {t.id for t in build_barter_graph(_nutmeg_opp(), plan,
                                            tail=MissionTail(kind="none"))}
    assert "barter" in ids
    assert not ids & {"sail_to_sell", "sail_route", "sell"}


def test_route_tail_runs_in_order():
    plan = _Plan(purchases={"Near": {"Coral": 1}})
    seen = []
    kinds = ("gather", "sell_surplus", "supply_verify", "sail_to_village", "barter",
             "sail_route", "sell")
    ex = {k: (lambda t: (seen.append(t.id), {"ok": True})[1]) for k in kinds}
    res = run_mission(build_barter_graph(_nutmeg_opp(), plan,
                                         tail=MissionTail(kind="route", value="R")),
                      COORDS, ex, start=COORDS["Home"])
    assert res.ok
    assert seen == ["gather:Near", "sell_surplus", "supply_verify", "sail_to_village",
                    "barter", "sail_route", "sell"]


def test_a_short_supply_stops_the_mission_before_the_village_leg():
    plan = _Plan(purchases={"Near": {"Coral": 1}})
    seen = []
    ex = {k: (lambda t: (seen.append(t.id), {"ok": True})[1])
          for k in ("gather", "sell_surplus", "sail_to_village", "barter",
                    "sail_to_sell", "sell")}
    ex["supply_verify"] = lambda t: {"ok": False, "reason": "supply 3.1d < 7.0d needed"}
    res = run_mission(build_barter_graph(_nutmeg_opp(), plan), COORDS, ex,
                      start=COORDS["Home"])
    assert not res.ok
    assert "supply" in res.reason
    assert "sail_to_village" not in seen


# ── Dynamic ordering: cheapest-from-current-position, varies with start ────────

def test_gather_order_is_nearest_first_and_varies_with_start():
    def graph():
        return [
            SubTask("gather:Near", "gather", "Near"),
            SubTask("gather:Mid", "gather", "Mid"),
            SubTask("gather:Far", "gather", "Far"),
        ]
    order = []
    def rec(task):
        order.append(task.location); return {"ok": True}

    run_mission(graph(), COORDS, {"gather": rec}, start=COORDS["Home"])
    assert order == ["Near", "Mid", "Far"]       # nearest-neighbour from Home

    order.clear()
    run_mission(graph(), COORDS, {"gather": rec}, start=COORDS["Far"])
    assert order == ["Far", "Mid", "Near"]       # order flips when starting far


def test_deps_respected_tail_after_all_gathers():
    opp = _nutmeg_opp(rounds=1)
    plan = _Plan(purchases={"Near": {"Coral": 1}, "Far": {"Ebony": 1}})
    seen = []
    ex = {k: (lambda t: (seen.append(t.id), {"ok": True})[1])
          for k in ("gather", "sell_surplus", "supply_verify", "sail_to_village",
                    "barter", "sail_to_sell", "sell")}
    res = run_mission(build_barter_graph(opp, plan), COORDS, ex, start=COORDS["Home"])
    assert res.ok
    # both gathers precede the village; barter precedes sell; sell is last.
    assert seen.index("sail_to_village") > seen.index("gather:Near")
    assert seen.index("sail_to_village") > seen.index("gather:Far")
    assert seen.index("sell") == len(seen) - 1
    assert seen.index("barter") < seen.index("sell")


# ── Recovery ──────────────────────────────────────────────────────────────────

def test_retry_then_success():
    calls = {"n": 0}
    def flaky(_t):
        calls["n"] += 1
        return {"ok": calls["n"] >= 2, "reason": "dialog hiccup"}
    res = run_mission([SubTask("g", "gather", "Near")], COORDS,
                      {"gather": flaky}, start=COORDS["Home"], max_retries=1)
    assert res.ok and calls["n"] == 2


def test_default_recover_aborts_with_reason():
    res = run_mission([SubTask("g", "gather", "Near")], COORDS,
                      {"gather": lambda t: {"ok": False, "reason": "boom"}},
                      start=COORDS["Home"], max_retries=0)
    assert not res.ok and "boom" in res.reason


def test_out_of_stock_reroutes_to_alternative_source():
    recipe = BarterRecipe(good="Box of Nutmeg",
                          inputs=[RecipeInput(material="Coral",
                                              source_ports=["Near", "Alt"])])
    # gather at 'Near' is out of stock; 'Alt' works.
    def ex(task):
        if task.location == "Near":
            return {"ok": False, "reason": "market out of stock"}
        return {"ok": True}
    g = [SubTask("gather:Near", "gather", "Near", params={"port": "Near",
                                                          "orders": {"Coral": 100}})]
    res = run_mission(g, COORDS, {"gather": ex}, start=COORDS["Home"],
                      recover=make_barter_recover(recipe), max_retries=0)
    assert res.ok
    assert any(s["id"].startswith("gather:Alt") and s["ok"] for s in res.steps)


def test_reroute_gives_up_when_no_alternative():
    recipe = BarterRecipe(good="X", inputs=[RecipeInput(material="Coral",
                                                        source_ports=["Near"])])
    g = [SubTask("gather:Near", "gather", "Near", params={"port": "Near",
                                                          "orders": {"Coral": 1}})]
    res = run_mission(g, COORDS, {"gather": lambda t: {"ok": False, "reason": "out of stock"}},
                      start=COORDS["Home"], recover=make_barter_recover(recipe), max_retries=0)
    assert not res.ok and "alternative" in res.reason


# ── Live-executor adapter shape (no execution → no device) ────────────────────

def test_live_executors_expose_all_kinds():
    from brain.barter_mission_live import make_live_executors
    execs = make_live_executors(object())
    assert set(execs) == {"gather", "sell_surplus", "supply_verify", "sail_to_village",
                          "barter", "sail_route", "sail_to_sell", "sell"}
    assert all(callable(f) for f in execs.values())


def test_every_kind_the_graph_emits_has_a_live_executor():
    """The invariant that matters: no graph shape can produce a sub-task the live
    executor table can't run (run_mission would fail it with 'no executor for kind')."""
    from brain.barter_mission_live import make_live_executors
    execs = make_live_executors(object())
    plan = _Plan(purchases={"Near": {"Coral": 1}})
    for tail in (None, MissionTail(kind="route", value="R"),
                 MissionTail(kind="sail", value="Edinburgh", sell_port="Edinburgh"),
                 MissionTail(kind="none")):
        for task in build_barter_graph(_nutmeg_opp(), plan, tail=tail):
            assert task.kind in execs, f"no executor for {task.kind!r}"
