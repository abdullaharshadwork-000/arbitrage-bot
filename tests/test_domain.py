"""Unit tests for arbicore.domain – Phase 1 foundational models.

These tests verify the contracts only. They do not touch exchange APIs,
risk limits, or execution paths.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from arbicore.domain import (
    AuditCategory,
    AuditEvent,
    Experience,
    OperatingMode,
    StrategyStatus,
    StrategyVersion,
    TradeProposal,
)


class OperatingModeTests(unittest.TestCase):
    def test_live_allows_real_orders(self):
        self.assertTrue(OperatingMode.LIVE.allows_real_orders)

    def test_non_live_modes_forbid_real_orders(self):
        for mode in (
            OperatingMode.RESEARCH,
            OperatingMode.SHADOW,
            OperatingMode.PAPER,
            OperatingMode.TESTNET,
        ):
            with self.subTest(mode=mode):
                self.assertFalse(mode.allows_real_orders)

    def test_research_has_no_exchange_data(self):
        self.assertFalse(OperatingMode.RESEARCH.allows_exchange_data)

    def test_other_modes_allow_exchange_data(self):
        for mode in (
            OperatingMode.SHADOW,
            OperatingMode.PAPER,
            OperatingMode.TESTNET,
            OperatingMode.LIVE,
        ):
            with self.subTest(mode=mode):
                self.assertTrue(mode.allows_exchange_data)


class StrategyVersionTests(unittest.TestCase):
    def test_create_and_serialize(self):
        sv = StrategyVersion(
            id="strat_001",
            name="cross_exchange",
            version="1.0.0",
            status=StrategyStatus.DRAFT,
            description="Initial version",
            parameters={"min_profit_pct": 0.15},
            created_by="test",
        )
        data = sv.as_dict()
        self.assertEqual(data["id"], "strat_001")
        self.assertEqual(data["status"], "draft")
        self.assertIn("created_at", data)
        self.assertEqual(data["parameters"]["min_profit_pct"], 0.15)

    def test_parent_version_links_genealogy(self):
        parent = StrategyVersion(
            id="strat_001", name="cross_exchange", version="1.0.0"
        )
        child = StrategyVersion(
            id="strat_002",
            name="cross_exchange",
            version="1.1.0",
            parent_version_id=parent.id,
            status=StrategyStatus.CANDIDATE,
        )
        self.assertEqual(child.parent_version_id, "strat_001")


class AuditEventTests(unittest.TestCase):
    def test_create_helper(self):
        event = AuditEvent.create(
            AuditCategory.RISK,
            "trade_rejected",
            component="RiskManager",
            symbol="BTC/USDT",
            mode=OperatingMode.PAPER,
            reason="daily loss limit reached",
            payload={"loss": 52.0},
        )
        self.assertTrue(event.id.startswith("aud_"))
        self.assertEqual(event.category, AuditCategory.RISK)
        self.assertEqual(event.event_type, "trade_rejected")
        self.assertEqual(event.reason, "daily loss limit reached")
        self.assertEqual(event.payload["loss"], 52.0)

        data = event.as_dict()
        self.assertEqual(data["category"], "risk")
        self.assertEqual(data["mode"], "paper")
        self.assertIn("timestamp", data)


class ExperienceTests(unittest.TestCase):
    def test_create_and_serialize(self):
        exp = Experience(
            id="exp_001",
            timestamp=datetime.now(timezone.utc),
            symbol="ETH/USDT",
            mode=OperatingMode.PAPER,
            strategy_id="cross_exchange",
            strategy_version="1.0.0",
            proposed_action="BUY",
            final_action="BUY",
            realized_pnl=Decimal("1.25"),
            fees=Decimal("0.40"),
        )
        data = exp.as_dict()
        self.assertEqual(data["mode"], "paper")
        self.assertEqual(data["realized_pnl"], "1.25")
        self.assertEqual(data["fees"], "0.40")


class TradeProposalTests(unittest.TestCase):
    def test_create_and_serialize(self):
        proposal = TradeProposal(
            id="prop_001",
            symbol="BTC/USDT",
            action="BUY",
            strategy_id="cross_exchange",
            strategy_version="1.0.0",
            market_regime="stable",
            regime_confidence=0.82,
            trade_confidence=0.71,
            requested_risk_fraction=Decimal("0.005"),
            reasoning_summary="Edge survived adaptive floor",
        )
        data = proposal.as_dict()
        self.assertEqual(data["action"], "BUY")
        self.assertEqual(data["requested_risk_fraction"], "0.005")
        self.assertIn("created_at", data)

    def test_no_trade_is_valid_action(self):
        proposal = TradeProposal(
            id="prop_002",
            symbol="BTC/USDT",
            action="NO_TRADE",
            strategy_id="cross_exchange",
            strategy_version="1.0.0",
            reasoning_summary="Insufficient confidence",
        )
        self.assertEqual(proposal.action, "NO_TRADE")


if __name__ == "__main__":
    unittest.main()
