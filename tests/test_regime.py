"""Unit tests for arbicore.regime – Phase 4 Market Regime detector."""

from __future__ import annotations

import unittest

from arbicore.features import FeatureEngine
from arbicore.regime import (
    REGIME_STRONG_BULL,
    REGIME_WARMING_UP,
    RegimeDetector,
)


class RegimeDetectorTests(unittest.TestCase):
    def setUp(self):
        self.features = FeatureEngine(min_bars=5)
        self.detector = RegimeDetector(min_bars=10)

    def test_warming_up_on_short_history(self):
        snap = self.features.compute("BTC/USDT", [100, 101, 102])
        decision = self.detector.classify(snap)
        self.assertEqual(decision.regime, REGIME_WARMING_UP)
        self.assertEqual(decision.confidence, 0.0)

    def test_strong_uptrend_classification(self):
        # Steady climb produces positive momentum
        prices = [100 + i * 0.8 for i in range(30)]
        snap = self.features.compute("ETH/USDT", prices)
        decision = self.detector.classify(snap)
        self.assertIn(decision.regime, {
            REGIME_STRONG_BULL,
            "WEAK_BULL_TREND",
            "VOLATILITY_EXPANSION",
        })
        self.assertGreater(decision.confidence, 0.0)
        self.assertTrue(decision.supporting)

    def test_decision_is_serializable(self):
        prices = [100 + i for i in range(25)]
        snap = self.features.compute("SOL/USDT", prices)
        decision = self.detector.classify(snap)
        data = decision.as_dict()
        self.assertIn("regime", data)
        self.assertIn("confidence", data)
        self.assertIn("supporting", data)


if __name__ == "__main__":
    unittest.main()
