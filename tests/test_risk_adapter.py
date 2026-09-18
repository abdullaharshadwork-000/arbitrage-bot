"""Tests for risk_adapter and decision_pipeline."""

from __future__ import annotations

import unittest
from decimal import Decimal
from types import SimpleNamespace

from arbicore.critic import CriticDecision
from arbicore.domain import OperatingMode, TradeProposal
from arbicore.live_bridge import OrderIntentBridge
from arbicore.risk import RiskManager
from arbicore.risk_adapter import RiskAdapter
from arbicore.decision_pipeline import DecisionPipeline


def _settings():
    return SimpleNamespace(
        max_daily_loss_usdt=Decimal("100"),
        max_consecutive_failures=5,
        max_position_notional_usdt=Decimal("500"),
        min_notional_usdt=Decimal("5"),
        max_orders_per_minute=30,
        halt_on_stranded_position=True,
    )


def _proposal(action="BUY"):
    return TradeProposal(
        id="prop_t",
        symbol="BTC/USDT",
        action=action,
        strategy_id="s1",
        strategy_version="1.0.0",
        trade_confidence=0.8,
        entry_price=Decimal("50000"),
        stop_loss=Decimal("49600"),
        take_profit=Decimal("50800"),
        requested_risk_fraction=Decimal("0.01"),
        expected_reward_risk=2.0,
    )


class RiskAdapterTests(unittest.TestCase):
    def setUp(self):
        self.rm = RiskManager(_settings())
        self.rm.update_equity(10_000)
        self.adapter = RiskAdapter(self.rm, equity=10_000.0, mode=OperatingMode.PAPER)

    def test_rejects_without_intent_risk(self):
        bridge = OrderIntentBridge(mode=OperatingMode.PAPER)
        result = bridge.build(
            _proposal("NO_TRADE"),
            CriticDecision(result="APPROVE", confidence=1.0),
        )
        self.assertFalse(result.accepted)

    def test_allows_when_risk_ok(self):
        bridge = OrderIntentBridge(mode=OperatingMode.PAPER)
        br = bridge.build(
            _proposal("BUY"),
            CriticDecision(result="APPROVE", confidence=0.9),
        )
        self.assertTrue(br.accepted)
        ar = self.adapter.evaluate(br.intent)
        self.assertTrue(ar.allowed)
        self.assertIsNotNone(ar.request)
        self.assertGreater(ar.request.notional_usdt, 0)

    def test_pipeline_end_to_end_paper(self):
        pipe = DecisionPipeline(self.rm, equity=10_000.0, mode=OperatingMode.PAPER)
        out = pipe.run(_proposal("BUY"))
        # Critic may WARN/REJECT based on missing features; either way no crash call
        self.assertIsNotNone(out.critique)
        self.assertIsNotNone(out.bridge)


if __name__ == "__main__":
    unittest.main()
