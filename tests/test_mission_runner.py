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
    # The gathers are unordered WITH RESPECT TO EACH OTHER, and both wait on the pre-gather
    # trim: gathering needs space, and a cluttered hold also hides the bought good's tile
    # below the fold (2026-08-26).
    assert g["gather:Near"].deps == ("trim_before_gather",)
    assert g["gather:Far"].deps == ("trim_before_gather",)
    assert "gather:Far" not in g["gather:Near"].deps
    assert g["trim_before_gather"].deps == ()
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
    assert seen == ["trim_before_gather", "gather:Near", "sell_surplus", "supply_verify", "sail_to_village",
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


def test_a_material_underfoot_is_not_sailed_away_from():
    """A GATHER LEG NAMES THE PORT THAT SOURCES ITS MATERIAL. Amsterdam has the Iron,
    Tripoli has the Candle — so a leg whose port is the one under our feet costs no voyage
    at all, and leaving it is wrong at any distance.

    Live 2026-08-29: the fleet stood in Amsterdam and the port read as 'Peking' (a 'Herring'
    label snapped to the nearest port at 0.62 similarity). Ranked by distance from Peking,
    Tripoli won, and the mission sailed away from the Iron it had come for.

    Ranking by distance would ALSO have picked Amsterdam here, at distance zero — which is
    exactly why the rule must be stated rather than left to fall out of the arithmetic. Said
    outright, a bad position read can no longer trade it away.
    """
    import types

    from brain.mission_runner import _same_port

    assert _same_port("Amsterdam", "amsterdam")
    assert _same_port("Lubeck", "Lübeck"), "the catalogue and the screen disagree on accents"
    assert not _same_port("Amsterdam", "Peking")
    assert not _same_port("Amsterdam", None)


def test_a_gather_leg_asks_for_everything_still_wanted():
    """THE SHELF IS THE AUTHORITY, NOT THE PLAN.

    `assign_purchases` pins each material to the FIRST port of the planned route that
    sources it. That held while `run_mission` walked the route in order; this runner
    re-decides the next leg from where the fleet actually is, so a diverged itinerary leaves
    the assignment describing a journey nobody took.

    Live 2026-08-29: Iron was pinned to Amsterdam because Amsterdam led the route. A misread
    port sent the fleet to Tripoli first, and at Barcelona — a known Iron source, Iron on the
    shelf — it bought only its assigned Matchlock Gun and left the Iron behind.
    """
    import types

    from brain.mission_runner import MissionRunner

    r = MissionRunner.__new__(MissionRunner)
    r.subtasks = [
        types.SimpleNamespace(id="gather:Amsterdam", kind="gather", done=False,
                              params={"port": "Amsterdam", "orders": {"Iron": 822}}),
        types.SimpleNamespace(id="gather:Barcelona", kind="gather", done=False,
                              params={"port": "Barcelona", "orders": {"Matchlock Gun": 411}}),
        types.SimpleNamespace(id="gather:Tripoli", kind="gather", done=True,
                              params={"port": "Tripoli", "orders": {"Candle": 934}}),
        types.SimpleNamespace(id="sell", kind="sell", done=False, params={}),
    ]

    # every material a pending gather leg wants — whichever port it was assigned to
    assert r._everything_still_wanted() == {"Iron": 822, "Matchlock Gun": 411}

    # A PARTIAL BUY SETTLES NOTHING. The result carries a total and a verdict, not a
    # per-good breakdown, so which material fell short is unknown — guessing strands one.
    r._settle_gathers(met=False)
    assert [t.id for t in r.subtasks if not t.done] == [
        "gather:Amsterdam", "gather:Barcelona", "sell"]

    # ...and a buy that met the whole list finishes every gather leg at once.
    r._settle_gathers(met=True)
    assert [t.id for t in r.subtasks if not t.done] == ["sell"]


def test_a_village_has_no_market_either():
    """ASHORE IS NOT ENOUGH. A village is ashore and has no market — its left menu is
    Explore / Gifting / Loot / Recruit Crew / Barter, with no building list to open. So
    ENTER_BUILDING has nothing to tap there and the tap changes nothing.

    Live 2026-08-30: a run started at Svear with the mission's optional trim first. The
    market intent went out at the village every tick — "already dispatched... nothing
    changed" — and the guard stopped the run before the barter it had sailed there for. The
    check knew a market leg cannot run AT SEA and stopped there.

    Optional legs are SKIPPED on this, not failed, so the mission moves on to the barter —
    which is the work a village actually has.
    """
    import types

    from brain.mission_runner import MissionRunner

    r = MissionRunner.__new__(MissionRunner)
    leg = types.SimpleNamespace(id="trim_before_gather", kind="sell_surplus", optional=True)

    for nowhere in ("village", "sea", "sea_cinematic"):
        assert r._can_run_here(leg, types.SimpleNamespace(state=nowhere)), nowhere
    for somewhere in ("port_overworld", "building:market"):
        assert r._can_run_here(leg, types.SimpleNamespace(state=somewhere)) is None, somewhere


def test_once_it_departs_for_the_village_there_is_no_more_gathering():
    """THE DEPARTURE IS THE LINE, NOT THE ARRIVAL (user, 2026-08-30).

    The trim and the checks all happen BEFORE the fleet leaves for the village; after that
    the mission is just the barter and the sail to sell. `mission_progress` already says so
    in its own phases — "Leaving this phase ends cargo checking for the rest of the task" —
    and being COMPANY-owned it survives a restart, so a run interrupted mid-voyage does not
    re-plan itself back into gathering.

    Standing in the village settles it too, as a backstop.

    Live 2026-08-30 without it: a run started at Svear with the Birch Tree already aboard,
    skipped the trim (no market in a village) and picked `gather:Amsterdam`, setting out to
    fetch materials it was carrying — then could not leave either, and the guard stopped it
    having done nothing at all.

    The mechanism: a village reads `port=None`, so "a leg underfoot wins" cannot fire, every
    distance collapses to 0.0, and `min()` returns whatever came first in the graph.
    """
    import types

    from brain.mission_runner import MissionRunner

    def _legs():
        mk = lambda i, k: types.SimpleNamespace(id=i, kind=k, done=False, params={},
                                                location=None, deps=())
        return [mk("trim_before_gather", "sell_surplus"), mk("gather:Amsterdam", "gather"),
                mk("gather:Barcelona", "gather"), mk("sell_surplus", "sell_surplus"),
                mk("supply_verify", "supply_verify"), mk("sail_to_village", "sail_to_village"),
                mk("barter", "barter"), mk("sail_to_sell", "sail_to_sell"), mk("sell", "sell")]

    at_village = MissionRunner.__new__(MissionRunner)
    at_village.subtasks = _legs()
    at_village._departed_for_the_village(types.SimpleNamespace(state="village"))
    assert [t.id for t in at_village.subtasks if not t.done] == [
        "barter", "sail_to_sell", "sell"]

    # ...and nowhere else settles anything: a port is where the approach still has work.
    at_port = MissionRunner.__new__(MissionRunner)
    at_port.subtasks = _legs()
    at_port._departed_for_the_village(types.SimpleNamespace(state="port_overworld"))
    assert all(not t.done for t in at_port.subtasks)

    # nor once the barter itself is finished — the tail must not be swept up with it
    done_bartering = MissionRunner.__new__(MissionRunner)
    done_bartering.subtasks = _legs()
    for t in done_bartering.subtasks:
        if t.kind == "barter":
            t.done = True
    done_bartering._departed_for_the_village(types.SimpleNamespace(state="village"))
    assert not done_bartering.subtasks[0].done, "nothing to settle once the barter is over"


def test_the_phase_settles_it_even_at_sea():
    """A run interrupted mid-voyage to the village must not re-plan into gathering.

    This is what keys the rule to the DEPARTURE rather than the arrival: at sea the screen
    says nothing about which leg the mission is on, but the phase does — and it is the phase
    that outlives the process.
    """
    import types
    from unittest.mock import patch

    from brain.mission_runner import MissionRunner

    mk = lambda i, k: types.SimpleNamespace(id=i, kind=k, done=False, params={},
                                            location=None, deps=())
    r = MissionRunner.__new__(MissionRunner)
    r.subtasks = [mk("gather:Amsterdam", "gather"), mk("sail_to_village", "sail_to_village"),
                  mk("barter", "barter"), mk("sell", "sell")]

    with patch("brain.mission_progress.at_least", return_value=True):
        r._departed_for_the_village(types.SimpleNamespace(state="sea"))
    assert [t.id for t in r.subtasks if not t.done] == ["barter", "sell"]
