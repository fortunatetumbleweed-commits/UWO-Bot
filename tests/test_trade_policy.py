"""Trade decision policy — pick the next action from observed state."""
import unittest

from brain.trade_policy import TradeState, decide_trade_action


class TradePolicyTests(unittest.TestCase):
    def test_sell_when_cargo_profits_here(self):
        s = TradeState("Port Royal", "London", cargo_used=3000, cargo_total=4108,
                       sellable_profit_here=["Sugar", "Rum"])
        a = decide_trade_action(s)
        self.assertEqual(a["op"], "sell")
        self.assertEqual(a["goods"], ["Sugar", "Rum"])

    def test_buy_when_room_and_profitable_for_dest(self):
        s = TradeState("London", "Port Royal", cargo_used=560, cargo_total=4108,
                       sellable_profit_here=[], buyable_for_dest=["Whisky", "Steel"])
        a = decide_trade_action(s)
        self.assertEqual(a["op"], "buy")
        self.assertEqual(a["destination"], "Port Royal")

    def test_already_loaded_goes_straight_to_sail(self):
        # the user's example: hold full of London goods (bound for Port Royal),
        # at London — none profitable to sell here, no room to buy → SAIL, not sell.
        s = TradeState("London", "Port Royal", cargo_used=3900, cargo_total=4108,
                       sellable_profit_here=[], buyable_for_dest=["Whisky"])
        a = decide_trade_action(s)
        self.assertEqual(a["op"], "sail")
        self.assertEqual(a["destination"], "Port Royal")

    def test_no_profitable_buys_and_nothing_to_sell_sails(self):
        s = TradeState("London", "Amsterdam", cargo_used=560, cargo_total=4108,
                       sellable_profit_here=[], buyable_for_dest=[])
        a = decide_trade_action(s)
        self.assertEqual(a["op"], "sail")

    def test_sell_takes_priority_over_buy(self):
        s = TradeState("Seville", "London", cargo_used=600, cargo_total=4108,
                       sellable_profit_here=["Wine"], buyable_for_dest=["Olive Oil"])
        self.assertEqual(decide_trade_action(s)["op"], "sell")

    def test_no_destination_is_done(self):
        s = TradeState("London", None, cargo_used=100, cargo_total=4108)
        self.assertEqual(decide_trade_action(s)["op"], "done")


if __name__ == "__main__":
    unittest.main()
