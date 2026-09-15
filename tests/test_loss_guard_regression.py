import unittest
from decimal import Decimal
from unittest.mock import patch

from arbicore.safety import ExecutionSafety


class LossBudgetSizingTests(unittest.TestCase):
    def test_exhausted_or_exceeded_budget_prevents_new_exposure(self):
        guard = ExecutionSafety()
        for budget in (0, "0", Decimal("0"), "-0.01", "-50"):
            with self.subTest(budget=budget):
                self.assertEqual(guard.dynamic_size(200, 1000, budget), 0)

    def test_missing_or_invalid_budget_prevents_new_exposure(self):
        guard = ExecutionSafety()
        for budget in (None, "", "unknown", "NaN", "sNaN", "Infinity", "-Infinity"):
            with self.subTest(budget=budget):
                self.assertEqual(guard.dynamic_size(200, 1000, budget), 0)

    def test_shrinking_budget_never_restores_the_full_order_size(self):
        guard = ExecutionSafety()
        sizes = [guard.dynamic_size(200, 1000, budget,
                                    worst_case_loss_pct="2.5")
                 for budget in ("10", "1", "0.01", "0", "-0.01")]
        self.assertEqual(sizes, [Decimal("200"), Decimal("40"),
                                 Decimal("0.4"), Decimal("0"), Decimal("0")])

    def test_missing_dashboard_balance_cannot_bypass_exhausted_daily_budget(self):
        import server
        for realized in (Decimal("-10"), Decimal("NaN"), None):
            with self.subTest(realized=realized), \
                    patch.object(server.risk_manager, "realized_today", realized), \
                    patch.object(server, "available_quote_balance", return_value=None):
                size = server.safe_candidate_size({"execution_mode": "real", "trade_size": 100,
                                                   "max_daily_loss": 10}, {"buy_exchange": "binance"})
            self.assertEqual(size, 0)


if __name__ == "__main__":
    unittest.main()
