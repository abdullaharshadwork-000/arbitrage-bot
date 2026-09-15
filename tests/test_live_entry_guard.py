"""Offline integration checks: account risk must veto actual execution paths."""
from decimal import Decimal
import copy
from unittest.mock import patch

import server
from tests.test_server_loop import ServerTestCase, FakeEngine, FakeFeed


class LiveEntryGuardTests(ServerTestCase):
    def setUp(self):
        saved_state = copy.deepcopy(server.state)
        super().setUp()
        def restore_state():
            server.state.clear()
            server.state.update(saved_state)
        self.addCleanup(restore_state)

    def test_missing_venue_balance_never_reaches_execution(self):
        server.state["config"]["execution_mode"] = "real"
        engine = FakeEngine(None)
        with patch.object(engine, "fetch_balances", side_effect=RuntimeError("offline")), \
                patch.object(server, "execute_candidate") as execute:
            self.run_one_scan(engine=engine)
        execute.assert_not_called()
        self.assertEqual(server.state["last_scan_status"], "account_risk_blocked")
        self.assertEqual(server.risk_manager.equity, 0)

    def test_over_cap_blocks_live_but_does_not_sell_holdings(self):
        server.state["config"].update(execution_mode="real", max_inventory_exposure_pct=5)
        engine = FakeEngine(None)
        with patch.object(server, "execute_candidate") as execute:
            self.run_one_scan(engine=engine)
        execute.assert_not_called()
        self.assertEqual(engine.closed, [])
        self.assertFalse(server.state["live_exposure"]["allowed"])
        self.assertEqual(server.risk_manager.equity, 2200)

    def test_balance_changes_after_scan_check_block_entry(self):
        server.state["config"]["execution_mode"] = "real"
        engine = FakeEngine(None)
        balances = engine.fetch_balances("binance")
        with patch.object(engine, "fetch_balances", side_effect=[balances, engine.fetch_balances("kucoin"),
                RuntimeError("account changed"), RuntimeError("account changed")]), \
                patch.object(server, "execute_candidate") as execute:
            self.run_one_scan(engine=engine)
        execute.assert_not_called()
        self.assertEqual(server.state["recent"][0]["limit"], "live_account_risk")

    def test_unrealized_drawdown_halts_even_without_opportunities(self):
        server.state["config"]["execution_mode"] = "real"
        server.risk_manager.update_equity(2300)
        with patch.object(server, "execute_candidate") as execute:
            self.run_one_scan(feed=FakeFeed(quotes={}), engine=FakeEngine(None))
        execute.assert_not_called()
        self.assertTrue(server.risk_manager.halted)
        self.assertEqual(server.risk_manager.realized_today, 0)
        self.assertFalse(server.state["running"])

    def test_failed_snapshot_preserves_last_known_equity(self):
        server.risk_manager.update_equity(2200)
        engine = FakeEngine(None)
        with patch.object(engine, "fetch_balances", side_effect=RuntimeError("offline")):
            report = server.refresh_live_risk(engine, server.state["config"], ["binance"], ["BTC/USDT"])
        self.assertFalse(report["allowed"])
        self.assertEqual(server.risk_manager.equity, 2200)
        self.assertFalse(server.risk_manager.halted)

    def test_confirmed_zero_equity_is_published_and_latches_drawdown(self):
        server.risk_manager.update_equity(2200)
        balances = {"binance": {"USDT": {"free": 0, "used": 0, "total": 0}}}
        valuation = {"free_usdt": 0, "used_usdt": 0, "total_usdt": 0}
        with patch.object(server, "collect_live_balances", return_value=(balances, valuation)):
            report = server.refresh_live_risk(FakeEngine(None), server.state["config"], ["binance"], [])
        self.assertFalse(report["allowed"])
        self.assertTrue(server.risk_manager.halted)
        self.assertEqual(server.state["live_portfolio_value"], 0)

    def test_sizing_reserves_inventory_headroom_and_equity_loss_budget(self):
        cfg = {"execution_mode": "real", "trade_size": 200, "max_daily_loss": 50}
        with patch.object(server, "available_quote_balance", return_value=1000):
            size = server.safe_candidate_size(cfg, {}, {"allowed": True, "headroom_usdt": 10})
            self.assertGreater(size, 0)
            self.assertLess(size, 10)
            server.risk_manager.peak_equity = Decimal("2200")
            server.risk_manager.equity = Decimal("2150.01")
            self.assertLess(server.safe_candidate_size(cfg, {}), 1)

    def test_invalid_exposure_setting_rejected(self):
        for value in (0, -1, 101, float("nan"), None):
            self.assertIn("max_inventory_exposure_pct", server.validate_config_update(
                {"max_inventory_exposure_pct": value}))
