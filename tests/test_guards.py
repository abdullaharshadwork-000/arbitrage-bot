"""Unit tests for arbicore.guards – Phase 1 live-mode gates."""

from __future__ import annotations

import unittest
from decimal import Decimal

from arbicore.config import REAL_TRADING_ACK, Settings
from arbicore.domain import OperatingMode
from arbicore.guards import LiveModeGuard, assert_real_trading_permitted


def _paper_settings(**overrides) -> Settings:
    base = dict(
        mode="live",
        execution_mode="paper",
        real_trading_ack="",
        exchanges=("binance",),
        symbols=("BTC/USDT",),
        trade_size_usdt=Decimal("20"),
        max_position_notional_usdt=Decimal("50"),
        max_daily_loss_usdt=Decimal("10"),
    )
    base.update(overrides)
    return Settings(**base)


def _real_ready_settings() -> Settings:
    return _paper_settings(
        execution_mode="real",
        real_trading_ack=REAL_TRADING_ACK,
        # Credentials will still be missing → real orders must still be refused
    )


class LiveModeGuardTests(unittest.TestCase):
    def test_live_data_requires_live_mode(self):
        guard = LiveModeGuard(_paper_settings(mode="demo"))
        decision = guard.can_use_live_data()
        self.assertFalse(decision)
        self.assertIn("mode", decision.reason)

    def test_live_data_allowed_when_mode_live(self):
        guard = LiveModeGuard(_paper_settings(mode="live"))
        self.assertTrue(guard.can_use_live_data())

    def test_real_orders_refused_without_ack(self):
        guard = LiveModeGuard(_paper_settings(execution_mode="real"))
        decision = guard.can_place_real_orders()
        self.assertFalse(decision)
        self.assertIn("acknowledgement", decision.reason.lower())

    def test_real_orders_refused_without_live_mode(self):
        guard = LiveModeGuard(
            _paper_settings(mode="demo", execution_mode="real", real_trading_ack=REAL_TRADING_ACK)
        )
        decision = guard.can_place_real_orders()
        self.assertFalse(decision)

    def test_real_orders_refused_when_credentials_missing(self):
        guard = LiveModeGuard(_real_ready_settings())
        decision = guard.can_place_real_orders()
        self.assertFalse(decision)
        self.assertIn("credentials", decision.reason.lower())

    def test_operating_mode_mapping(self):
        paper = LiveModeGuard(_paper_settings())
        self.assertEqual(paper.operating_mode(), OperatingMode.PAPER)

        research = LiveModeGuard(_paper_settings(mode="demo"))
        self.assertEqual(research.operating_mode(), OperatingMode.RESEARCH)

    def test_assert_real_trading_permitted_raises(self):
        with self.assertRaises(RuntimeError):
            assert_real_trading_permitted(_paper_settings())

    def test_audit_event_is_produced(self):
        guard = LiveModeGuard(_paper_settings())
        event = guard.audit_real_trading_check()
        self.assertEqual(event.category.value, "safety")
        self.assertEqual(event.event_type, "real_trading_check")
        self.assertIn("allowed", event.payload)


if __name__ == "__main__":
    unittest.main()
