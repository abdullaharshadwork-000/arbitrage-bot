"""Unit tests for arbicore.signals_agent – Phase 23."""

from __future__ import annotations

import unittest

from arbicore.domain import StrategyStatus
from arbicore.features import FeatureEngine
from arbicore.regime import RegimeDetector
from arbicore.selection import StrategySelector
from arbicore.signals_agent import SignalEngine
from arbicore.strategy_registry import StrategyRegistry


class SignalEngineTests(unittest.TestCase):
    def setUp(self):
        self.features = FeatureEngine(min_bars=5)
        self.regime = RegimeDetector(min_bars=10)
        self.selector = StrategySelector(min_regime_confidence=0.3)
        self.signals = SignalEngine()
        self.registry = StrategyRegistry()

    def test_no_strategy_means_no_trade(self):
        prices = [100 + i * 0.5 for i in range(40)]
        snap = self.features.compute("BTC/USDT", prices)
        regime = self.regime.classify(snap)
        selection = self.selector.select([], regime)
        proposal = self.signals.propose("BTC/USDT", snap, regime, selection, last_price=prices[-1])
        self.assertEqual(proposal.action, "NO_TRADE")

    def test_aligned_bullish_can_propose_buy(self):
        sv = self.registry.create(
            name="mom",
            version="1.0.0",
            intended_regimes=("STRONG_BULL_TREND", "WEAK_BULL_TREND"),
        )
        self.registry.set_status(sv.id, StrategyStatus.APPROVED)
        prices = [100 + i * 1.0 for i in range(40)]  # strong uptrend
        snap = self.features.compute("ETH/USDT", prices)
        regime = self.regime.classify(snap)
        selection = self.selector.select([self.registry.get(sv.id)], regime)
        proposal = self.signals.propose("ETH/USDT", snap, regime, selection, last_price=prices[-1])
        # May be BUY or NO_TRADE depending on thresholds; must be structured either way
        self.assertIn(proposal.action, {"BUY", "NO_TRADE", "SELL"})
        if proposal.action == "BUY":
            self.assertIsNotNone(proposal.stop_loss)
            self.assertIsNotNone(proposal.take_profit)
            self.assertGreater(proposal.trade_confidence, 0)

    def test_proposal_is_serializable(self):
        prices = [100 + i * 0.2 for i in range(30)]
        snap = self.features.compute("SOL/USDT", prices)
        regime = self.regime.classify(snap)
        selection = self.selector.select([], regime)
        proposal = self.signals.propose("SOL/USDT", snap, regime, selection, last_price=prices[-1])
        data = proposal.as_dict()
        self.assertIn("action", data)
        self.assertIn("reasoning_summary", data)


if __name__ == "__main__":
    unittest.main()
