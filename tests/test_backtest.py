"""Unit tests for arbicore.backtest – Phase 11."""

from __future__ import annotations

import unittest

from arbicore.backtest import Backtester, BacktestConfig
from arbicore.strategy_registry import StrategyRegistry


class BacktesterTests(unittest.TestCase):
    def setUp(self):
        self.registry = StrategyRegistry()
        self.strategy = self.registry.create(
            name="momentum_demo",
            version="0.1.0",
            intended_regimes=("STRONG_BULL_TREND", "WEAK_BULL_TREND"),
        )
        self.bt = Backtester(BacktestConfig(min_bars=20, initial_capital=10_000))

    def test_insufficient_bars(self):
        result = self.bt.run(self.strategy, "BTC/USDT", [100, 101, 102])
        self.assertEqual(result.meta.get("status"), "insufficient_bars")

    def test_runs_on_synthetic_uptrend(self):
        prices = [100 + i * 0.5 for i in range(80)]
        result = self.bt.run(self.strategy, "ETH/USDT", prices)
        self.assertEqual(result.bars, 80)
        self.assertIsInstance(result.total_pnl, float)
        self.assertIsInstance(result.win_rate, float)
        self.assertIn("trade_count", result.meta)

    def test_result_is_serializable(self):
        prices = [100 + i * 0.3 for i in range(50)]
        result = self.bt.run(self.strategy, "SOL/USDT", prices)
        data = result.as_dict()
        self.assertIn("strategy_id", data)
        self.assertIn("trades", data)


if __name__ == "__main__":
    unittest.main()
