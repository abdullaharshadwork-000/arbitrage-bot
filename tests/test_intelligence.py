import unittest

from arbicore.intelligence import OpportunityIntelligence, strategy_evidence


def quotes(price, spread=0.02):
    return {
        "BTC/USDT": {
            "binance": {"bid": price - spread, "ask": price + spread},
            "kucoin": {"bid": price + 1 - spread, "ask": price + 1 + spread},
        }
    }


def candidate(edge=0.50):
    return {
        "symbol": "BTC/USDT",
        "buy_exchange": "binance",
        "sell_exchange": "kucoin",
        "net_pct": edge,
        "legs": 2,
    }


class OpportunityIntelligenceTests(unittest.TestCase):
    def test_real_candidate_waits_for_auditable_warmup(self):
        model = OpportunityIntelligence(min_observations=4)
        model.observe(quotes(100.0), 0.1)

        result = model.evaluate(candidate(), 0.15, 0.25, 0.65)

        self.assertFalse(result.qualified)
        self.assertIn("warming up", result.reason)
        self.assertEqual(result.observations, 0)

    def test_stable_high_edge_qualifies_after_warmup(self):
        model = OpportunityIntelligence(min_observations=4)
        for price in (100.0, 100.001, 100.002, 100.003, 100.004):
            model.observe(quotes(price), 0.05)

        result = model.evaluate(candidate(0.50), 0.15, 0.25, 0.65)

        self.assertTrue(result.qualified)
        self.assertEqual(result.regime, "stable")
        self.assertGreater(result.predicted_edge_pct, 0.15)
        self.assertGreaterEqual(result.confidence, 0.65)

    def test_stressed_market_is_rejected_even_with_a_large_edge(self):
        model = OpportunityIntelligence(
            min_observations=4, stressed_volatility_pct=0.10)
        for price in (100, 101, 99, 102, 98, 103):
            model.observe(quotes(price), 0.1)

        result = model.evaluate(candidate(2.0), 0.15, 0.25, 0.65)

        self.assertFalse(result.qualified)
        self.assertEqual(result.regime, "stressed")
        self.assertIn("stressed", result.reason)

    def test_snapshot_exposes_bounded_next_scan_interval(self):
        model = OpportunityIntelligence(min_observations=3)
        for price in (100, 100.1, 100.2, 100.3):
            model.observe(quotes(price), 0.1)

        forecast = model.snapshot()["forecasts"][0]

        self.assertLessEqual(forecast["lower_95_pct"], forecast["next_scan_change_pct"])
        self.assertGreaterEqual(forecast["upper_95_pct"], forecast["next_scan_change_pct"])

    def test_strategy_advisor_requires_samples_and_positive_lower_bound(self):
        too_few = strategy_evidence([
            {"strategy": "triangular", "profit_usdt": 1.0} for _ in range(5)
        ], minimum_samples=30)
        qualified = strategy_evidence([
            {"strategy": "triangular", "profit_usdt": 0.5} for _ in range(30)
        ], minimum_samples=30)

        self.assertIsNone(too_few["recommended_strategy"])
        self.assertEqual(qualified["recommended_strategy"], "triangular")


if __name__ == "__main__":
    unittest.main()
