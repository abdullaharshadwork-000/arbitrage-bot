import unittest
from decimal import Decimal

from arbicore.safety import ExecutionSafety


class ExecutionSafetyTests(unittest.TestCase):
    def test_healthy_feed_is_allowed(self):
        guard = ExecutionSafety()
        result = guard.observe_feed(
            {"BTC/USDT": {"binance": {"bid": 100, "ask": 101, "age_ms": 25}}},
            0.1,
        )
        self.assertTrue(result)
        self.assertEqual(guard.feed_failures, 0)

    def test_three_stale_snapshots_latch_the_guard(self):
        guard = ExecutionSafety(max_quote_age_ms=100, max_consecutive_feed_failures=3)
        quotes = {"BTC/USDT": {"binance": {
            "bid": 100, "ask": 101, "age_ms": 101,
        }}}
        self.assertFalse(guard.observe_feed(quotes, 0.1))
        self.assertFalse(guard.observe_feed(quotes, 0.1))
        result = guard.observe_feed(quotes, 0.1)
        self.assertFalse(result)
        self.assertTrue(guard.halted)
        self.assertEqual(result.limit, "stale_feed")

    def test_one_bad_snapshot_recovers_without_a_latched_halt(self):
        guard = ExecutionSafety(max_consecutive_feed_failures=3)
        self.assertFalse(guard.observe_feed({}, 0.1))
        self.assertTrue(guard.observe_feed(
            {"BTC/USDT": {"binance": {"bid": 100, "ask": 101}}}, 0.1))
        self.assertEqual(guard.feed_failures, 0)
        self.assertFalse(guard.halted)

    def test_dynamic_size_can_only_reduce_the_operator_size(self):
        guard = ExecutionSafety()
        self.assertEqual(
            guard.dynamic_size(10, 100, 5, visible_depth=1000), Decimal("10"))
        self.assertEqual(
            guard.dynamic_size(10, 12, 5, visible_depth=1000), Decimal("5.88"))
        self.assertLess(
            guard.dynamic_size(10, 100, 5, visible_depth=1000, volatility_pct=4),
            Decimal("10"))

    def test_persistently_bad_realized_edge_halts(self):
        guard = ExecutionSafety(degradation_window=3, min_realized_edge_ratio="0.5")
        self.assertTrue(guard.record_execution("1", "0.2"))
        self.assertTrue(guard.record_execution("1", "0.4"))
        result = guard.record_execution("1", "0.3")
        self.assertFalse(result)
        self.assertTrue(guard.halted)
        self.assertEqual(result.limit, "execution_degradation")


if __name__ == "__main__":
    unittest.main()
