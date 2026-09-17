"""Unit tests for arbicore.validation – Phases 12–13."""

from __future__ import annotations

import unittest

from arbicore.strategy_registry import StrategyRegistry
from arbicore.validation import StressTester, WalkForwardValidator


class WalkForwardTests(unittest.TestCase):
    def setUp(self):
        self.registry = StrategyRegistry()
        self.strategy = self.registry.create(name="wf_demo", version="0.1.0")
        self.validator = WalkForwardValidator(
            train_bars=30, test_bars=15, step_bars=15, min_profitable_ratio=0.0
        )

    def test_insufficient_data(self):
        report = self.validator.run(self.strategy, "BTC/USDT", [100.0] * 20)
        self.assertFalse(report.passed)
        self.assertIn("insufficient", report.reason)

    def test_runs_on_long_series(self):
        prices = [100 + i * 0.2 for i in range(120)]
        report = self.validator.run(self.strategy, "ETH/USDT", prices)
        self.assertGreater(report.total_windows, 0)
        self.assertIn("windows", report.as_dict())


class StressTests(unittest.TestCase):
    def setUp(self):
        self.registry = StrategyRegistry()
        self.strategy = self.registry.create(name="stress_demo", version="0.1.0")
        self.tester = StressTester(max_degradation_pct=100.0)

    def test_fee_spike_runs(self):
        prices = [100 + i * 0.3 for i in range(80)]
        report = self.tester.run_fee_spike(self.strategy, "BTC/USDT", prices)
        self.assertIn("fee_x", report.scenario)
        self.assertIsInstance(report.degradation_pct, float)

    def test_slippage_spike_runs(self):
        prices = [100 + i * 0.3 for i in range(80)]
        report = self.tester.run_slippage_spike(self.strategy, "SOL/USDT", prices)
        self.assertIn("slippage_x", report.scenario)


if __name__ == "__main__":
    unittest.main()
