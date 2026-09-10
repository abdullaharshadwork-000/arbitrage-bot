"""Route selection must maximize fee-adjusted returns, not raw spreads."""
import unittest
from decimal import Decimal

import arbitrage_bot as bot


class ProfitPrecisionTests(unittest.TestCase):
    def test_lower_raw_spread_can_have_higher_net_profit(self):
        quotes = {
            "expensive": {"ask": 100, "bid": 99},
            "cheap": {"ask": 100.1, "bid": 100},
            "seller": {"ask": 102, "bid": 101},
        }
        fees = {"BTC/USDT": 0.001, ("expensive", "BTC/USDT"): 0.02}
        result = bot.find_opportunity(quotes, fees, "BTC/USDT", min_profit=0.1)
        self.assertIsNotNone(result)
        self.assertEqual(result[:2], ("cheap", "seller"))

    def test_exact_threshold_is_accepted_and_just_below_is_rejected(self):
        quotes = {"a": {"ask": "100", "bid": "99"},
                  "b": {"ask": "102", "bid": "100.15"}}
        self.assertIsNotNone(bot.find_opportunity(quotes, 0, min_profit="0.15"))
        quotes["b"]["bid"] = "100.149999999999999999"
        self.assertIsNone(bot.find_opportunity(quotes, 0, min_profit="0.15"))

    def test_invalid_prices_do_not_become_opportunities(self):
        self.assertIsNone(bot.find_opportunity({
            "a": {"ask": float("nan"), "bid": 0},
            "b": {"ask": 100, "bid": float("inf")}}, min_profit=0.1))

    def test_triangle_matches_decimal_fee_calculation(self):
        quotes = {"BTC/USDT": {"a": {"ask": "10000.12345678"}},
                  "ETH/BTC": {"a": {"ask": "0.05001234"}},
                  "ETH/USDT": {"a": {"bid": "510.12345678"}}}
        result = bot.calculate_triangular_cycle(quotes, "a", "200", [0.001, 0.002, 0.003])
        expected = (Decimal("200") * Decimal("0.999") / Decimal("10000.12345678")
                    * Decimal("0.998") / Decimal("0.05001234")
                    * Decimal("510.12345678") * Decimal("0.997"))
        self.assertEqual(result["final_usdt"], float(expected))
        self.assertEqual(result["profit_usdt"], float(expected - Decimal("200")))
