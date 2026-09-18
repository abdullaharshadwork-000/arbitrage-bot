"""Critic + heuristic scorer integration tests."""

from __future__ import annotations

import unittest
from decimal import Decimal

from arbicore.critic import CriticAgent
from arbicore.domain import TradeProposal
from arbicore.features import FeatureSnapshot
from arbicore.ml_scorer import HeuristicScorer


def _proposal():
    return TradeProposal(
        id="p1",
        symbol="BTC/USDT",
        action="BUY",
        strategy_id="s1",
        strategy_version="1.0.0",
        trade_confidence=0.8,
        entry_price=Decimal("50000"),
        stop_loss=Decimal("49500"),
        take_profit=Decimal("51000"),
        requested_risk_fraction=Decimal("0.01"),
        expected_reward_risk=2.0,
    )


class CriticScorerTests(unittest.TestCase):
    def test_scorer_standalone(self):
        s = HeuristicScorer().score({"momentum_10": 0.02, "rsi_14": 55}, action="BUY")
        self.assertGreaterEqual(s.score, 0.0)
        self.assertLessEqual(s.score, 1.0)

    def test_critic_includes_scorer_meta(self):
        features = FeatureSnapshot(
            symbol="BTC/USDT",
            timestamp=0.0,
            features={"momentum_10": -0.05, "rsi_14": 80, "realized_vol": 0.01},
        )
        decision = CriticAgent().review(_proposal(), features=features)
        self.assertIn("heuristic_scorer", decision.meta)
        self.assertIn(decision.result, ("APPROVE", "WARN", "REJECT"))


if __name__ == "__main__":
    unittest.main()
