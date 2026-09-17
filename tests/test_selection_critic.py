"""Unit tests for selection + critic – Phases 6 & 7."""

from __future__ import annotations

import unittest
from decimal import Decimal

from arbicore.critic import CriticAgent
from arbicore.domain import StrategyStatus, StrategyVersion, TradeProposal
from arbicore.regime import RegimeDecision
from arbicore.selection import StrategySelector
from arbicore.strategy_registry import StrategyRegistry


class StrategySelectorTests(unittest.TestCase):
    def setUp(self):
        self.registry = StrategyRegistry()
        self.selector = StrategySelector(min_regime_confidence=0.40)

    def test_no_trade_when_no_candidates(self):
        regime = RegimeDecision(regime="STABLE", confidence=0.8)
        result = self.selector.select([], regime)
        self.assertEqual(result.action, "NO_TRADE")

    def test_no_trade_on_low_regime_confidence(self):
        sv = self.registry.create(name="x", version="1.0.0")
        self.registry.set_status(sv.id, StrategyStatus.APPROVED)
        regime = RegimeDecision(regime="UNCERTAIN", confidence=0.2)
        result = self.selector.select([self.registry.get(sv.id)], regime)
        self.assertEqual(result.action, "NO_TRADE")

    def test_selects_approved_strategy(self):
        sv = self.registry.create(
            name="cross_exchange",
            version="1.0.0",
            intended_regimes=("STRONG_BULL_TREND",),
        )
        self.registry.set_status(sv.id, StrategyStatus.APPROVED)
        regime = RegimeDecision(regime="STRONG_BULL_TREND", confidence=0.85)
        result = self.selector.select([self.registry.get(sv.id)], regime)
        self.assertEqual(result.action, "USE_STRATEGY")
        self.assertEqual(result.strategy_id, sv.id)


class CriticAgentTests(unittest.TestCase):
    def setUp(self):
        self.critic = CriticAgent()

    def test_no_trade_always_approved(self):
        proposal = TradeProposal(
            id="p1",
            symbol="BTC/USDT",
            action="NO_TRADE",
            strategy_id="s1",
            strategy_version="1.0.0",
        )
        decision = self.critic.review(proposal)
        self.assertEqual(decision.result, "APPROVE")

    def test_rejects_low_confidence(self):
        proposal = TradeProposal(
            id="p2",
            symbol="BTC/USDT",
            action="BUY",
            strategy_id="s1",
            strategy_version="1.0.0",
            trade_confidence=0.20,
            entry_price=Decimal("50000"),
            stop_loss=Decimal("49000"),
            take_profit=Decimal("52000"),
        )
        decision = self.critic.review(proposal)
        self.assertEqual(decision.result, "REJECT")
        codes = {o.code for o in decision.objections}
        self.assertIn("low_trade_confidence", codes)

    def test_rejects_missing_stop(self):
        proposal = TradeProposal(
            id="p3",
            symbol="ETH/USDT",
            action="BUY",
            strategy_id="s1",
            strategy_version="1.0.0",
            trade_confidence=0.80,
            entry_price=Decimal("3000"),
        )
        decision = self.critic.review(proposal)
        self.assertEqual(decision.result, "REJECT")
        codes = {o.code for o in decision.objections}
        self.assertIn("missing_stop", codes)


if __name__ == "__main__":
    unittest.main()
