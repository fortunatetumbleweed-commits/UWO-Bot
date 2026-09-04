"""What may be staged into the sell basket."""


def test_a_tile_with_no_price_is_not_a_good():
    """LIVE 2026-08-30 at Lisboa, in the middle of a successful sale:

        [Lisboa] load-to-sell Birch Tree @ (570,556) (profit/u 9667)
        [Lisboa] load-to-sell Im glad    @ (730,355) (profit/u None)
        [market] sold at Lisboa: ['Birch Tree', 'Im glad']

    "Im glad" is another player's CHAT MESSAGE. It was read as a good and tapped into the
    basket beside the cargo the mission had crossed the map to sell.

    Nothing downstream could have stopped it: `select_sellable` filtered on NAME only, and
    `goal="clear"` sells at a loss deliberately, so the loss test does not apply. An unpriced
    tile and a loss-making one are different things — only the second is a decision.

    Skipping is also the safer failure. If a real good's price is unreadable this pass it
    stays aboard and says so, which beats tapping unidentified text on the screen.
    """
    import types

    from actions.sell_goods import select_sellable

    goods = [
        types.SimpleNamespace(name="Birch Tree", profit_per_unit=9667, tap_x=570, tap_y=556),
        types.SimpleNamespace(name="Im glad", profit_per_unit=None, tap_x=730, tap_y=355),
        types.SimpleNamespace(name="Candle", sell_price=604, tap_x=100, tap_y=100),
    ]

    for goal in ("clear", "profit"):
        kept = [g.name for g in select_sellable(goods, goal=goal, keep=())]
        assert kept == ["Birch Tree", "Candle"], goal

    # a price in ANY of the fields a tile can carry it in counts as priced
    priced = types.SimpleNamespace(name="Iron", buy_price=88, tap_x=1, tap_y=1)
    assert [g.name for g in select_sellable([priced], goal="clear", keep=())] == ["Iron"]
