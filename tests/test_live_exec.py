"""LiveExecutor tests – mock place_fn only, never hits an exchange."""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from arbicore.live_exec import LiveExecutor, live_exec_enabled
from arbicore.risk_adapter import ApprovedOrderRequest


def _req(**kwargs):
    base = dict(
        id="aor_test",
        intent_id="intent1",
        symbol="BTC/USDT",
        side="buy",
        order_type="market",
        notional_usdt=100.0,
        risk_fraction=0.01,
        entry_price=50000.0,
        stop_loss=49000.0,
        take_profit=52000.0,
        strategy_id="s1",
        strategy_version="1.0.0",
        mode="live",
        risk_reason="allowed",
    )
    base.update(kwargs)
    return ApprovedOrderRequest(**base)


class LiveExecFlagTests(unittest.TestCase):
    def test_flag_default_off(self):
        env = {k: v for k, v in os.environ.items() if k != "ARBICORE_AGENT_LIVE_EXEC"}
        self.assertFalse(live_exec_enabled(env))

    def test_flag_on(self):
        self.assertTrue(live_exec_enabled({"ARBICORE_AGENT_LIVE_EXEC": "1"}))


class LiveExecutorTests(unittest.TestCase):
    def setUp(self):
        os.environ["ARBICORE_AGENT_LIVE_EXEC"] = "1"

    def tearDown(self):
        os.environ.pop("ARBICORE_AGENT_LIVE_EXEC", None)

    def test_executes_with_mock_place_fn(self):
        calls = []

        def place_fn(symbol, side, qty):
            calls.append((symbol, side, qty))
            return {"order_id": "oid1", "filled_quantity": qty, "average_price": 50000.0}

        ex = LiveExecutor(place_fn, canary_fraction=0.05, min_notional=1.0)
        result = ex.execute(_req())
        self.assertTrue(result.executed)
        self.assertEqual(result.order_id, "oid1")
        self.assertAlmostEqual(result.canary_fraction, 0.05)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "BTC/USDT")
        self.assertEqual(calls[0][1], "buy")

    def test_blocked_without_flag(self):
        os.environ.pop("ARBICORE_AGENT_LIVE_EXEC", None)

        def place_fn(symbol, side, qty):
            raise AssertionError("should not place")

        result = LiveExecutor(place_fn).execute(_req())
        self.assertFalse(result.executed)
        self.assertIn("LIVE_EXEC", result.reject_reason)

    def test_blocked_by_guard(self):
        guard = MagicMock()
        guard.can_place_real_orders.return_value = MagicMock(
            allowed=False, reason="no ack"
        )

        def place_fn(symbol, side, qty):
            raise AssertionError("should not place")

        result = LiveExecutor(place_fn, live_guard=guard).execute(_req())
        self.assertFalse(result.executed)
        self.assertIn("LiveModeGuard", result.reject_reason)


if __name__ == "__main__":
    unittest.main()
