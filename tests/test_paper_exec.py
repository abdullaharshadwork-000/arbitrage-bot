"""Unit tests for arbicore.paper_exec – Phase 24."""

from __future__ import annotations

import unittest
from decimal import Decimal

from arbicore.critic import CriticDecision
from arbicore.domain import OperatingMode, TradeProposal
from arbicore.paper_exec import PaperExecutor


def _proposal(action="BUY", confidence=0.8):
    return TradeProposal(
        id="prop_test",
        symbol="BTC/USDT",
        action=action,
        strategy_id="s1",
        strategy_version="1.0.0",
        trade_confidence=confidence,
        entry_price=Decimal("50000"),
        stop_loss=Decimal("49600"),
        take_profit=Decimal("50800"),
        requested_risk_fraction=Decimal("0.01"),
        expected_reward_risk=2.0,
    )


class PaperExecutorTests(unittest.TestCase):
    def setUp(self):
        self.exec = PaperExecutor(equity=10_000.0, mode=OperatingMode.PAPER)

    def test_rejects_critic_reject(self):
        result = self.exec.execute(
            _proposal(), CriticDecision(result="REJECT", confidence=0.9)
        )
        self.assertFalse(result.accepted)
        self.assertIn("REJECT", result.reject_reason)

    def test_rejects_warn(self):
        result = self.exec.execute(
            _proposal(), CriticDecision(result="WARN", confidence=0.5)
        )
        self.assertFalse(result.accepted)

    def test_rejects_no_trade(self):
        result = self.exec.execute(
            _proposal(action="NO_TRADE"),
            CriticDecision(result="APPROVE", confidence=1.0),
        )
        self.assertFalse(result.accepted)

    def test_fills_on_approve(self):
        result = self.exec.execute(
            _proposal(),
            CriticDecision(result="APPROVE", confidence=0.8),
            last_price=50_000.0,
        )
        self.assertTrue(result.accepted)
        self.assertIsNotNone(result.fill)
        self.assertEqual(result.fill.action, "BUY")
        self.assertGreater(result.fill.quantity, 0)
        self.assertEqual(len(self.exec.fills), 1)

    def test_cannot_construct_in_live_mode(self):
        with self.assertRaises(ValueError):
            PaperExecutor(mode=OperatingMode.LIVE)


if __name__ == "__main__":
    unittest.main()
