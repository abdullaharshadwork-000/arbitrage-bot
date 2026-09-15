"""What the server layer has to get right once real money is involved.

Four things are proved here that no unit test on arbicore can prove, because
they are properties of the loop and the HTTP layer rather than of the maths:

  * the scan loop does not hold `state_lock` while an order is in flight, so
    /api/state and /api/emergency-stop still answer during a trade;
  * a rebuild - a reset, or a structural config change - waits for the scan
    thread instead of swapping the feed out from under a half-done trade;
  * a risk limit stops the loop and leaves the reason visible, instead of
    clearing the error at the end of the same scan that set it;
  * the trades table migrates in place, so a database written by the previous
    release still loads.

The database and the CSV are redirected to a temporary directory before the
server module is imported. Nothing here touches the operator's own arbicore.db
or trades.csv.
"""

import contextlib
import os
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

_TMP = Path(tempfile.mkdtemp(prefix="arbserver_"))
os.environ["ARBICORE_DB"] = str(_TMP / "import.db")

import arbitrage_bot as bot           # noqa: E402
import server                          # noqa: E402
from arbicore import alerts, intelligence, orders, risk      # noqa: E402
from arbicore.money import D                    # noqa: E402

QUOTES = {
    "BTC/USDT": {
        "binance": {"bid": 10000.0, "ask": 10001.0},
        "kucoin": {"bid": 10060.0, "ask": 10061.0},
    },
}


def probe_lock(timeout=1.0):
    """Could /api/state be served right now? Answered from another thread."""
    outcome = []

    def grab():
        acquired = server.state_lock.acquire(timeout=timeout)
        outcome.append(acquired)
        if acquired:
            server.state_lock.release()

    worker = threading.Thread(target=grab)
    worker.start()
    worker.join(timeout + 2)
    return bool(outcome and outcome[0])


class FakeFeed:
    def __init__(self, quotes=None, symbols=("BTC/USDT",)):
        self.quotes = QUOTES if quotes is None else quotes
        self.symbols = list(symbols)
        self.routes = []
        self.route_candidates = 0
        self.rejected_quotes = {}
        self.last_fetch_seconds = 0.02
        self.calls = 0

    def get_quotes(self):
        self.calls += 1
        return self.quotes


class FakeWallet:
    """Books a fixed profit, like PaperWallet, without modelling balances."""

    def __init__(self, profit=1.0, decline=False, on_execute=None):
        self.profit = profit
        self.decline = decline
        self.on_execute = on_execute
        self.calls = []

    def execute_arbitrage(self, buy_ex, sell_ex, symbol, size, ask, bid, fee):
        self.calls.append((buy_ex, sell_ex, symbol, size, ask, bid, fee))
        if self.on_execute is not None:
            self.on_execute()
        if self.decline:
            return None
        return {}, size + self.profit

    def total_value(self, prices):
        return 3000.0


class FakeEngine:
    """A real engine that only ever raises, for the unhedged path."""

    def __init__(self, error):
        self.error = error
        self.clients = {"binance": object(), "kucoin": object()}
        self.closed = []

    def taker_fee(self, exchange):
        return 0.001

    def fetch_balances(self, exchange, currencies=None):
        return {"USDT": {"free": 1000, "used": 0, "total": 1000},
                "BTC": {"free": 1, "used": 0, "total": 1}}

    def value_balances_usdt(self, exchange, balances):
        for coin, values in balances.items():
            price = 1 if coin == "USDT" else 100
            values["value_free_usdt"] = values["free"] * price
            values["value_used_usdt"] = values["used"] * price
        return {"free_usdt": 1100, "used_usdt": 0, "total_usdt": 1100}

    def execute_arbitrage(self, *args, **kwargs):
        raise self.error

    def place_market_sell(self, exchange, symbol, quantity):
        self.closed.append((exchange, symbol, quantity))
        return {"id": "close-1", "clientOrderId": "close-client-1"}

    def normalize_amount(self, exchange, symbol, quantity):
        return float(quantity)

    def fetch_ticker(self, exchange, symbol):
        return {"bid": 100.0, "ask": 101.0}

    def check_order_book(self, *args):
        return 100.0

    def _confirm_fill(self, exchange, symbol, side, quantity, order):
        return orders.Fill(
            exchange, symbol, side, order["id"], order["clientOrderId"],
            "closed", D(quantity), D(quantity), D("100"), D(quantity) * D("100"),
            reconciled=True)

    def _dust_tolerance(self, exchange, symbol, quantity):
        return 0.0


class ServerTestCase(unittest.TestCase):
    """A clean server: fresh database, fresh risk manager, silent notifier."""

    def setUp(self):
        self.db_path = _TMP / f"{self.id().rsplit('.', 1)[-1]}.db"
        self.csv_path = _TMP / f"{self.id().rsplit('.', 1)[-1]}.csv"
        self._saved = {
            "db": server.DB_FILE, "log": bot.LOG_FILE,
            "feed": server.feed, "wallet": server.wallet,
            "engine": server.real_engine, "symbols": server.active_symbols,
            "notifier": server.notifier, "risk": server.risk_manager,
            "intelligence": server.market_intelligence,
            "thread": server._thread, "real_flag": bot.REAL_TRADING_ENABLED,
        }
        server.DB_FILE = self.db_path
        bot.LOG_FILE = str(self.csv_path)
        bot.init_csv()          # the header the real startup path writes
        server.initialize_database()

        server.state.update({
            "running": False, "scan_count": 0, "trades_count": 0,
            "start_value": 3000.0,
            "attempts_count": 0, "total_profit": 0.0, "recent": [],
            "trades": [], "unhedged_positions": [], "error": None,
            "chart_series": [], "latest_cycle": None, "quotes": {},
            "mid_prices": {}, "risk": {}, "alerts": {}, "startup_check": None,
            "live_exposure": {},
            "last_scan_started_at": None, "last_scan_completed_at": None,
            "last_scan_duration_seconds": None, "last_scan_status": "paused",
            "last_scan_message": "Engine is paused.",
        })
        server.state["config"].update({
            "mode": "live", "execution_mode": "paper",
            "strategy": "cross_exchange", "real_trading_enabled": False,
            "trade_size": 200.0, "fee": 0.001, "min_profit": 0.15,
            "max_slippage": 0.25, "interval": 0.0, "gap_chance": 0.0,
            "max_daily_loss": 50.0, "max_position_notional": 400.0,
            "max_inventory_exposure_pct": 50.0,
            "max_consecutive_failures": 3, "max_orders_per_minute": 20,
            "intelligence_enabled": True, "min_model_confidence": 0.65,
            "triangular_routes": [],
        })
        server.state["active_exchanges"] = ["binance", "kucoin"]
        server.active_symbols = ["BTC/USDT"]
        server.notifier = alerts.Notifier(webhook_url="", console=None)
        server.risk_manager = risk.RiskManager(server.risk_settings())
        server.market_intelligence = intelligence.OpportunityIntelligence()
        server._thread = None
        server._stop_flag.clear()
        server._emergency_stop.clear()

    def tearDown(self):
        server._stop_flag.set()
        server.DB_FILE = self._saved["db"]
        bot.LOG_FILE = self._saved["log"]
        server.feed = self._saved["feed"]
        server.wallet = self._saved["wallet"]
        server.real_engine = self._saved["engine"]
        server.active_symbols = self._saved["symbols"]
        server.notifier = self._saved["notifier"]
        server.risk_manager = self._saved["risk"]
        server.market_intelligence = self._saved["intelligence"]
        server._thread = self._saved["thread"]
        bot.REAL_TRADING_ENABLED = self._saved["real_flag"]

    def client(self):
        """A test client that carries the session token, like the dashboard's
        SameSite cookie does in a browser."""
        client = server.app.test_client()
        client.environ_base["HTTP_X_ARBICORE_TOKEN"] = server.API_TOKEN
        return client

    def run_one_scan(self, feed=None, wallet=None, engine=None):
        """Run exactly one iteration of scan_loop, in this thread.

        The loop is stopped from inside its own sleep, so the scan runs to
        completion - publishing, persisting and all - and then exits.
        """
        server.feed = feed or FakeFeed()
        server.wallet = wallet if wallet is not None else FakeWallet()
        server.real_engine = engine
        server._stop_flag.clear()
        # Existing loop tests target execution and recovery behavior. Give the
        # independent confidence gate a stable, fully warmed quote history so
        # those tests reach the boundary they are intended to exercise.
        server.market_intelligence = intelligence.OpportunityIntelligence()
        for _ in range(server.market_intelligence.min_observations + 1):
            server.market_intelligence.observe(server.feed.quotes, 0.02)

        def stop_after(_seconds):
            server._stop_flag.set()

        with mock.patch.object(server.time, "sleep", stop_after):
            server.scan_loop()


class TestScanLoopLocking(ServerTestCase):
    """The regression that matters most: the lock is free during a trade."""

    def test_signal_save_failure_restores_wallet_and_stops(self):
        from arbicore.paper import PaperAccount
        paper = PaperAccount(["binance"], ["BTC/USDT"], 20000,
                             {"BTC/USDT": 100}, "signal_trend")
        feed = FakeFeed()
        feed.clients = {"binance": object()}
        server.state["config"].update(strategy="signal_trend", execution_mode="paper")
        server.state["active_exchanges"] = ["binance"]
        def tick(*args, **kwargs):
            paper.ledger.debit("binance", "USDT", 100)
            paper.signal_state["position"] = {"symbol": "BTC/USDT"}
            return {}, {"status": "filled"}
        with mock.patch.object(server.signals, "paper_tick", side_effect=tick), \
             mock.patch.object(server, "persist_trade", side_effect=RuntimeError("disk unavailable")):
            self.run_one_scan(feed=feed, wallet=paper)
        self.assertEqual(paper.total_value({}), 20000)
        self.assertNotIn("position", paper.signal_state)
        self.assertFalse(server.state["running"])
        self.assertIn("Last committed", server.state["error"])

    def test_three_empty_real_feeds_latch_safety_halt(self):
        server.state["config"]["execution_mode"] = "real"
        guard = server.safety.ExecutionSafety()
        with mock.patch.object(server, "execution_safety", guard):
            for _ in range(3):
                self.run_one_scan(feed=FakeFeed(quotes={}), engine=FakeEngine(None))
            self.assertTrue(guard.halted)
            self.assertEqual(server.state["last_scan_status"], "safety_halt")
            self.assertFalse(server.state["running"])

    def test_paper_equity_loss_blocks_new_trades(self):
        server.state["start_value"] = 3100.0
        paper = FakeWallet()
        self.run_one_scan(wallet=paper)
        self.assertEqual(paper.calls, [])
        self.assertEqual(server.state["last_scan_status"], "safety_halt")
        self.assertIn("equity loss", server.state["error"])
        self.assertEqual(server.state["paper_portfolio_value"], 3000.0)

    def test_state_lock_is_available_while_an_order_is_in_flight(self):
        seen = []
        wallet = FakeWallet(on_execute=lambda: seen.append(probe_lock()))
        self.run_one_scan(wallet=wallet)
        self.assertEqual(len(seen), 1, "the trade never ran")
        self.assertTrue(
            seen[0],
            "state_lock was held while the order was being placed, so "
            "/api/state and /api/emergency-stop would have blocked on it")

    def test_emergency_stop_answers_while_an_order_is_in_flight(self):
        replies = []

        def stop_mid_trade():
            client = self.client()
            replies.append(client.post("/api/emergency-stop").get_json())

        self.run_one_scan(wallet=FakeWallet(on_execute=stop_mid_trade))
        self.assertEqual(len(replies), 1)
        self.assertTrue(replies[0]["ok"])
        self.assertTrue(server._emergency_stop.is_set())

    def test_a_scan_publishes_quotes_and_feed_health(self):
        feed = FakeFeed()
        self.run_one_scan(feed=feed)
        self.assertEqual(feed.calls, 1)
        self.assertEqual(server.state["quotes"], QUOTES)
        self.assertAlmostEqual(server.state["mid_prices"]["BTC/USDT"],
                               (10000.5 + 10060.5) / 2, places=6)
        self.assertEqual(server.state["feed_health"]["symbols"], 1)
        self.assertEqual(server.state["feed_health"]["fetch_seconds"], 0.02)
        self.assertEqual(server.state["scan_count"], 1)
        self.assertEqual(server.state["attempts_count"], 1)
        self.assertEqual(server.state["last_scan_status"], "trade_executed")
        self.assertIsNotNone(server.state["last_scan_completed_at"])

    def test_missing_live_quotes_are_reported_not_logged_as_no_opportunity(self):
        empty_feed = FakeFeed(quotes={})

        self.run_one_scan(feed=empty_feed)

        self.assertEqual(server.state["scan_count"], 1)
        self.assertEqual(server.state["last_scan_status"], "feed_unavailable")
        self.assertIn("retry automatically", server.state["last_scan_message"])
        self.assertEqual(server.state["feed_health"]["status"], "warming_up")
        self.assertFalse(any(item.get("type") == "miss"
                             for item in server.state["recent"]))


class TestTradeRecording(ServerTestCase):

    def test_a_paper_trade_lands_in_state_the_database_and_the_csv(self):
        self.run_one_scan(wallet=FakeWallet(profit=1.25))

        self.assertEqual(server.state["trades_count"], 1)
        trade = server.state["trades"][0]
        self.assertEqual(trade["symbol"], "BTC/USDT")
        self.assertEqual(trade["buy_exchange"], "binance")
        self.assertEqual(trade["sell_exchange"], "kucoin")
        self.assertEqual(trade["buy_price"], 10001.0)
        self.assertEqual(trade["sell_price"], 10060.0)
        self.assertEqual(trade["profit_usdt"], 1.25)
        self.assertEqual(trade["execution_mode"], "paper")
        self.assertEqual(trade["strategy"], "cross_exchange")
        self.assertEqual(trade["status"], "filled")
        # Paper execution has no orders, so it must not report ids.
        self.assertIsNone(trade["buy_order_id"])
        self.assertIsNone(trade["sell_order_id"])
        self.assertIsNone(trade["middle_order_id"])
        self.assertEqual(server.state["recent"][0]["type"], "hit")

        with sqlite3.connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT symbol, profit_usdt, execution_mode, strategy, status "
                "FROM trades").fetchall()
        self.assertEqual(rows, [("BTC/USDT", 1.25, "paper", "cross_exchange",
                                 "filled")])

        written = self.csv_path.read_text().strip().splitlines()
        self.assertEqual(len(written), 2, written)   # header plus one trade

    def test_a_scan_with_no_spread_records_a_miss_and_no_trade(self):
        flat = {"BTC/USDT": {"binance": {"bid": 10000.0, "ask": 10001.0},
                             "kucoin": {"bid": 10000.0, "ask": 10001.0}}}
        wallet = FakeWallet()
        self.run_one_scan(feed=FakeFeed(quotes=flat), wallet=wallet)
        self.assertEqual(wallet.calls, [])
        self.assertEqual(server.state["trades_count"], 0)
        self.assertEqual(server.state["attempts_count"], 1)
        self.assertEqual(server.state["recent"][0],
                         {"type": "miss",
                          "time": server.state["recent"][0]["time"],
                          "scan_num": 1})


class TestRiskGate(ServerTestCase):

    def test_an_oversized_trade_is_blocked_without_halting_the_loop(self):
        server.state["config"]["trade_size"] = 500.0     # cap is 400
        server.risk_manager.settings = server.risk_settings()
        wallet = FakeWallet()
        self.run_one_scan(wallet=wallet)

        self.assertEqual(wallet.calls, [], "an over-cap trade was still sent")
        blocked = server.state["recent"][0]
        self.assertEqual(blocked["type"], "blocked")
        self.assertEqual(blocked["limit"], "position_notional")
        self.assertIn("per-position cap", blocked["reason"])
        self.assertFalse(server.risk_manager.halted)
        self.assertIsNone(server.state["error"])

    def test_a_loss_past_the_daily_limit_halts_the_loop_and_says_why(self):
        wallet = FakeWallet(profit=-60.0)                 # limit is 50
        self.run_one_scan(wallet=wallet)

        self.assertEqual(len(wallet.calls), 1, "the first trade must still run")
        self.assertTrue(server.risk_manager.halted)
        self.assertEqual(server.risk_manager.halt_limit, risk.HALT_DAILY_LOSS)
        self.assertIn("daily_loss_limit", server.state["error"])
        self.assertFalse(server.state["running"])
        self.assertTrue(server._stop_flag.is_set())
        self.assertEqual(server.state["risk"]["halt_limit"], risk.HALT_DAILY_LOSS)

    def test_a_halted_risk_manager_refuses_to_start(self):
        server.risk_manager.halt(risk.HALT_DAILY_LOSS, "down too far today")
        # Test the already-owned worker's risk gate, not a context switch that
        # legitimately loads a different account's persisted risk state.
        with mock.patch.object(server, "require_engine_owner", return_value=None):
            response = self.client().post("/api/start")
        self.assertEqual(response.status_code, 409)
        self.assertIn("daily_loss_limit", response.get_json()["error"])
        self.assertFalse(server.state["running"])

    def test_resume_needs_confirmation_and_then_clears_the_halt(self):
        server.risk_manager.halt(risk.HALT_DAILY_LOSS, "down too far today")
        client = self.client()
        self.assertEqual(client.post("/api/risk/resume", json={}).status_code, 400)
        self.assertTrue(server.risk_manager.halted)

        response = client.post("/api/risk/resume",
                               json={"confirmation": "RESUME_AFTER_HALT"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(server.risk_manager.halted)
        self.assertIsNone(server.state["error"])

    def test_repeated_vetoes_for_one_reason_collapse_into_a_single_row(self):
        """A standing limit refuses every candidate of every scan. Sixty copies
        of one sentence would push the completed trades out of the blotter."""
        with server.state_lock:
            for symbol in ("BTC/USDT", "ETH/USDT", "SOL/USDT",
                           "XRP/USDT", "DOGE/USDT", "ADA/USDT", "LTC/USDT"):
                server._add_recent({
                    "type": "blocked",
                    "time": "2026-08-31T03:21:00",
                    "symbol": symbol,
                    "limit": "order_rate",
                    "reason": "20 orders in the last minute, at the 20/min cap",
                })

        self.assertEqual(len(server.state["recent"]), 1)
        row = server.state["recent"][0]
        self.assertEqual(row["count"], 7)
        self.assertEqual(row["symbols"],
                         ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT",
                          "DOGE/USDT", server.BLOCKED_SYMBOL_OVERFLOW])

    def test_a_veto_for_a_different_reason_starts_its_own_row(self):
        with server.state_lock:
            server._add_recent({"type": "blocked", "time": "t", "symbol": "BTC/USDT",
                                "limit": "order_rate", "reason": "at the cap"})
            server._add_recent({"type": "blocked", "time": "t", "symbol": "BTC/USDT",
                                "limit": "position_notional",
                                "reason": "over the per-position cap"})
            server._add_recent({"type": "hit", "time": "t", "symbol": "BTC/USDT"})
            server._add_recent({"type": "blocked", "time": "t", "symbol": "BTC/USDT",
                                "limit": "position_notional",
                                "reason": "over the per-position cap"})

        kinds = [(e["type"], e.get("limit")) for e in server.state["recent"]]
        self.assertEqual(kinds, [("blocked", "position_notional"),
                                 ("hit", None),
                                 ("blocked", "position_notional"),
                                 ("blocked", "order_rate")])
        self.assertNotIn("count", server.state["recent"][0])


class TestLaunch(ServerTestCase):
    """Both launchers share one bind-and-announce path."""

    def free_port(self):
        with contextlib.closing(socket.socket()) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def test_pick_port_skips_one_that_is_already_taken(self):
        taken = socket.socket()
        self.addCleanup(taken.close)
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        busy = taken.getsockname()[1]
        free = self.free_port()

        self.assertEqual(server.pick_port("127.0.0.1", [busy, free]), free)

    def test_pick_port_reports_no_usable_port_rather_than_guessing(self):
        taken = socket.socket()
        self.addCleanup(taken.close)
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        self.assertIsNone(
            server.pick_port("127.0.0.1", [taken.getsockname()[1]]))

    def test_the_live_launcher_uses_the_same_serve_path(self):
        """start_live.py used to call app.run(port=5000) itself, which skipped
        the port fallback and never printed the session token."""
        source = (Path(server.__file__).with_name("start_live.py")
                  .read_text(encoding="utf-8"))
        self.assertIn("server.bootstrap()", source)
        self.assertIn("server.serve()", source)
        self.assertNotIn("app.run", source)


class TestUnhedgedPosition(ServerTestCase):
    """One leg filled, the other did not. The expensive case."""

    def unhedged(self, confirmed=True):
        return bot.UnhedgedPositionError(
            "binance", "kucoin", "BTC/USDT", 0.02,
            {"id": "buy-1"}, RuntimeError("kucoin rejected the sell"),
            quantity_confirmed=confirmed)

    def test_a_stranded_position_halts_the_loop_and_is_persisted(self):
        server.state["config"]["execution_mode"] = "real"
        self.run_one_scan(wallet=None, engine=FakeEngine(self.unhedged()))

        position = server.state["unhedged_positions"][0]
        self.assertEqual(position["status"], "manual_recovery_required")
        self.assertEqual(position["recovery_exchange"], "binance")
        self.assertEqual(position["quantity"], 0.02)
        self.assertTrue(position["quantity_confirmed"])
        self.assertEqual(position["buy_order_id"], "buy-1")

        self.assertIn("Manual recovery required", server.state["error"])
        self.assertTrue(server._stop_flag.is_set())
        self.assertFalse(server.state["running"])

        # The risk manager latches, so the loop cannot be restarted around coin
        # of unknown size until a human clears it.
        self.assertEqual(len(server.risk_manager.stranded), 1)
        self.assertEqual(server.risk_manager.stranded[0]["currency"], "BTC")
        self.assertTrue(server.risk_manager.halted)

        # And it survives a restart of the process.
        self.assertEqual(
            [p["buy_order_id"] for p in server.load_persisted_recovery()],
            ["buy-1"])

    def test_an_unconfirmed_quantity_is_recorded_as_an_upper_bound(self):
        server.state["config"]["execution_mode"] = "real"
        self.run_one_scan(wallet=None,
                          engine=FakeEngine(self.unhedged(confirmed=False)))
        position = server.state["unhedged_positions"][0]
        self.assertFalse(position["quantity_confirmed"])
        self.assertIn("UNCONFIRMED", server.state["error"])

    def test_a_recovery_without_an_exchange_id_uses_the_client_id(self):
        server.state["config"]["execution_mode"] = "real"
        failure = bot.UnhedgedPositionError(
            "binance", "kucoin", "BTC/USDT", 0.02,
            {"clientOrderId": "arbi-timeout-1"},
            RuntimeError("exchange reply was lost"),
            quantity_confirmed=False)
        self.run_one_scan(wallet=None, engine=FakeEngine(failure))
        self.assertEqual(
            server.state["unhedged_positions"][0]["buy_order_id"],
            "arbi-timeout-1")
        self.assertEqual(
            server.load_persisted_recovery()[0]["buy_order_id"],
            "arbi-timeout-1")

    def test_closing_a_recovery_clears_the_stranded_latch(self):
        server.state["config"]["execution_mode"] = "real"
        bot.REAL_TRADING_ENABLED = True
        engine = FakeEngine(self.unhedged())
        self.run_one_scan(wallet=None, engine=engine)
        self.assertTrue(server.risk_manager.stranded)

        response = self.client().post(
            "/api/recovery/close",
            json={"buy_order_id": "buy-1",
                  "confirmation": "CLOSE_UNHEDGED_POSITION"})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(engine.closed, [("binance", "BTC/USDT", 0.02)])
        self.assertEqual(server.risk_manager.stranded, [],
                         "resume() would refuse forever with this still set")
        self.assertEqual(server.load_persisted_recovery(), [])
        self.assertEqual(server.state["unhedged_positions"], [])

    def test_an_unconfirmed_fill_cannot_be_closed_automatically(self):
        server.state["config"]["execution_mode"] = "real"
        bot.REAL_TRADING_ENABLED = True
        engine = FakeEngine(self.unhedged(confirmed=False))
        self.run_one_scan(wallet=None, engine=engine)

        response = self.client().post(
            "/api/recovery/close",
            json={"buy_order_id": "buy-1",
                  "confirmation": "CLOSE_UNHEDGED_POSITION"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("upper bound", response.get_json()["error"])
        self.assertEqual(engine.closed, [],
                         "a market sell of an unconfirmed size was sent")


class TestDurableSafetyState(ServerTestCase):
    def test_risk_checkpoint_round_trips_through_the_database(self):
        previous_owner = server.state.get("owner_user_id")
        self.addCleanup(server.state.__setitem__, "owner_user_id", previous_owner)
        server.state["owner_user_id"] = 1
        server.risk_manager.record_success(D("-1.23456789"))
        server.risk_manager.record_stranded(
            "binance", "BTC", D("0.00012"), "second leg failed")

        self.assertTrue(server.persist_risk_state())
        payload = server.load_risk_state(1)
        restored = risk.RiskManager(server.risk_settings())
        self.assertTrue(restored.restore_state(payload))
        self.assertEqual(restored.realized_today, D("-1.23456789"))
        self.assertEqual(restored.stranded[0]["quantity"], D("0.00012"))
        self.assertTrue(restored.halted)

    def test_a_corrupt_risk_checkpoint_fails_closed(self):
        with server.db() as connection:
            connection.execute(
                "INSERT INTO risk_states (user_id, payload, updated_at) "
                "VALUES (1, 'not-json', 'now')")
        server.activate_user_context(1)
        self.assertTrue(server.risk_manager.halted)
        self.assertEqual(server.risk_manager.halt_limit, risk.HALT_RECONCILE)
        self.assertIn("could not be restored", server.risk_manager.halt_reason)

    def test_a_corrupt_recovery_record_remains_visible_and_non_closeable(self):
        with server.db() as connection:
            connection.execute(
                "INSERT INTO recovery_positions "
                "(buy_order_id, created_at, payload, user_id, status, updated_at) "
                "VALUES ('recovery-corrupt', 'now', 'not-json', 1, 'open', 'now')")
        positions = server.load_persisted_recovery(1)
        self.assertEqual(positions[0]["buy_order_id"], "recovery-corrupt")
        self.assertFalse(positions[0]["quantity_confirmed"])
        self.assertIn("unreadable", positions[0]["error"])

    def test_worker_lease_cannot_be_stolen_until_it_is_stale(self):
        previous_lease = server.WORKER_LEASE_ID
        self.addCleanup(setattr, server, "WORKER_LEASE_ID", previous_lease)
        server.WORKER_LEASE_ID = "worker-a"
        self.assertTrue(server.acquire_worker_lease(1))

        server.WORKER_LEASE_ID = "worker-b"
        self.assertFalse(server.acquire_worker_lease(1))
        self.assertFalse(server.heartbeat_worker_lease(1))

        with server.db() as connection:
            connection.execute(
                "UPDATE worker_leases SET heartbeat_at = ? WHERE user_id = 1",
                (time.time() - server.WORKER_LEASE_TTL_SECONDS - 1,))
        self.assertTrue(server.acquire_worker_lease(1))
        self.assertTrue(server.heartbeat_worker_lease(1))

    def test_a_late_open_event_cannot_regress_a_terminal_order(self):
        previous_owner = server.state.get("owner_user_id")
        self.addCleanup(server.state.__setitem__, "owner_user_id", previous_owner)
        server.state["owner_user_id"] = 1
        server.persist_order_intent(
            "arbi-terminal", "binance", "BTC/USDT", "buy", "0.001")
        server.update_order_intent(
            "arbi-terminal", "closed",
            {"id": "exchange-1", "filled": "0.001", "average": "100000"})
        server.update_order_intent(
            "arbi-terminal", "reconciling", {"id": "exchange-1"})

        with server.db() as connection:
            status = connection.execute(
                "SELECT status FROM order_intents WHERE client_order_id = ?",
                ("arbi-terminal",)).fetchone()[0]
            events = connection.execute(
                "SELECT event_type FROM order_events WHERE client_order_id = ?",
                ("arbi-terminal",)).fetchall()
        self.assertEqual(status, "closed")
        self.assertIn(("reconciling",), events)


class TestThreadLifecycle(ServerTestCase):

    def test_stop_scan_thread_waits_and_forgets_a_finished_thread(self):
        done = threading.Event()
        server._thread = threading.Thread(target=done.wait, args=(5,))
        server._thread.start()
        done.set()
        self.assertTrue(server.stop_scan_thread(timeout=5))
        self.assertIsNone(server._thread)

    def test_stop_scan_thread_reports_a_thread_that_will_not_leave(self):
        release = threading.Event()
        stuck = threading.Thread(target=release.wait, args=(30,), daemon=True)
        server._thread = stuck
        stuck.start()
        try:
            self.assertFalse(server.stop_scan_thread(timeout=0.1))
            self.assertIs(server._thread, stuck,
                          "a thread still running must not be forgotten")
        finally:
            release.set()
            stuck.join(5)

    def test_a_rebuild_is_refused_while_the_scan_thread_is_busy(self):
        release = threading.Event()
        stuck = threading.Thread(target=release.wait, args=(30,), daemon=True)
        server._thread = stuck
        stuck.start()
        # The real join logic, only with a timeout a test can wait for: the
        # route's own default is bound at import time, so patching the constant
        # would not reach it.
        real_stop = server.stop_scan_thread
        try:
            with mock.patch.object(server, "stop_scan_thread",
                                   lambda timeout=None: real_stop(0.1)):
                response = self.client().post(
                    "/api/config", json={"execution_mode": "real"})
            self.assertEqual(response.status_code, 409)
            self.assertIn("reconciled", response.get_json()["error"])
            self.assertEqual(server.state["config"]["execution_mode"], "paper",
                             "the mode changed while a trade was in flight")
        finally:
            release.set()
            stuck.join(5)

    def test_a_numeric_knob_applies_without_touching_the_thread(self):
        response = self.client().post("/api/config",
                                                 json={"min_profit": 0.4})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(server.state["config"]["min_profit"], 0.4)

    def test_a_risk_limit_change_reaches_the_risk_manager(self):
        response = self.client().post(
            "/api/config", json={"max_daily_loss": 12.5,
                                 "max_consecutive_failures": 2})
        self.assertEqual(response.status_code, 200)
        settings = server.risk_manager.settings
        self.assertEqual(float(settings.max_daily_loss_usdt), 12.5)
        self.assertEqual(settings.max_consecutive_failures, 2)
        self.assertEqual(server.state["config"]["max_consecutive_failures"], 2)


class TestConfigValidation(ServerTestCase):
    """A config change is judged as a whole configuration, not field by field.

    Every one of these settings passes on its own. What makes them wrong is
    another setting, and the cost of accepting them is paid later and somewhere
    else: a size above the position cap turns every scan into a veto, a size
    below the exchange minimum is rejected by the venue mid-trade, and a
    cross-exchange strategy with one venue simply never finds anything.
    """

    def post(self, payload):
        return self.client().post("/api/config", json=payload)

    def test_a_trade_size_above_the_position_cap_is_refused(self):
        response = self.post({"trade_size": 900})
        self.assertEqual(response.status_code, 400)
        self.assertIn("max_position_notional", response.get_json()["error"])
        self.assertEqual(server.state["config"]["trade_size"], 200.0,
                         "a rejected update must not be half applied")

    def test_the_hard_trade_size_cap_cannot_be_raised_from_the_dashboard(self):
        """`ABSOLUTE_MAX_TRADE_SIZE_USDT` is the ceiling no config can lift, so
        raising the position limit to match does not buy a bigger trade."""
        response = self.post({"trade_size": 20000,
                              "max_position_notional": 20000})
        self.assertEqual(response.status_code, 400)
        self.assertIn("hard cap", response.get_json()["error"])

    def test_a_trade_size_below_the_exchange_minimum_is_refused(self):
        response = self.post({"trade_size": 4})
        self.assertEqual(response.status_code, 400)
        self.assertIn("minimum", response.get_json()["error"])

    def test_raising_the_size_and_the_cap_together_is_allowed(self):
        """Validation runs on the result, so changes that are only valid
        together are accepted together."""
        response = self.post({"trade_size": 600, "max_position_notional": 800})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(server.state["config"]["trade_size"], 600.0)
        self.assertEqual(
            float(server.risk_manager.settings.max_position_notional_usdt), 800.0)

    def test_cross_exchange_with_a_single_venue_is_refused(self):
        response = self.post({"strategy": "cross_exchange",
                              "exchanges": ["binance"]})
        self.assertEqual(response.status_code, 400)
        self.assertIn("two exchanges", response.get_json()["error"])
        self.assertEqual(server.state["active_exchanges"], ["binance", "kucoin"])

    def test_one_venue_is_fine_for_triangular(self):
        self.addCleanup(setattr, bot, "TRADING_STRATEGY", bot.TRADING_STRATEGY)
        self.addCleanup(setattr, bot, "EXCHANGES", list(bot.EXCHANGES))
        with mock.patch.object(server, "init_engine"):
            response = self.post({"strategy": "triangular",
                                  "exchanges": ["kucoin"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(server.state["active_exchanges"], ["kucoin"])

    def test_selecting_real_execution_is_left_to_the_real_trading_gate(self):
        """The validator pins execution to paper: refusing "real" here would
        replace the acknowledgement gate's explanation with a generic one, and
        the dashboard's readiness panel needs the selection to make it."""
        self.addCleanup(setattr, bot, "EXECUTION_MODE", bot.EXECUTION_MODE)
        with mock.patch.object(server, "init_engine"):
            response = self.post({"execution_mode": "real"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(server.state["config"]["execution_mode"], "real")

    def test_real_execution_cannot_disable_decision_intelligence(self):
        response = self.post({
            "execution_mode": "real", "intelligence_enabled": False,
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("mandatory", response.get_json()["error"])


class TestDatabase(ServerTestCase):

    def test_the_trades_table_migrates_in_place(self):
        """A database written by the previous release still loads, and its rows
        survive: the new columns are added, not recreated around."""
        legacy = _TMP / "legacy.db"
        with sqlite3.connect(legacy) as connection:
            connection.executescript("""
                CREATE TABLE trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    time TEXT NOT NULL, symbol TEXT NOT NULL,
                    buy_exchange TEXT, sell_exchange TEXT,
                    trade_size_usdt REAL, profit_usdt REAL, net_profit_pct REAL,
                    buy_order_id TEXT, middle_order_id TEXT, sell_order_id TEXT,
                    status TEXT
                );
            """)
            connection.execute(
                "INSERT INTO trades (time, symbol, profit_usdt) VALUES (?, ?, ?)",
                ("2025-01-01T00:00:00", "ETH/USDT", 0.5))

        server.DB_FILE = legacy
        server.initialize_database()

        with sqlite3.connect(legacy) as connection:
            columns = {row[1] for row in
                       connection.execute("PRAGMA table_info(trades)")}
            rows = connection.execute(
                "SELECT symbol, profit_usdt, execution_mode FROM trades"
            ).fetchall()
        self.assertLessEqual(set(server.TRADE_COLUMNS), columns)
        self.assertEqual(rows, [("ETH/USDT", 0.5, None)],
                         "the pre-release row must survive with NULL for the "
                         "fields it never had")

        server.persist_trade({
            "time": "2026-01-01T00:00:00", "symbol": "BTC/USDT",
            "buy_exchange": "binance", "sell_exchange": "kucoin",
            "trade_size_usdt": 200.0, "profit_usdt": 1.0,
            "net_profit_pct": 0.4, "execution_mode": "paper",
            "strategy": "cross_exchange", "status": "filled",
        })
        with sqlite3.connect(legacy) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 2)

    def test_migrating_twice_is_a_no_op(self):
        server.initialize_database()
        server.initialize_database()      # would raise "duplicate column name"

    def test_the_db_helper_commits_and_then_closes(self):
        with server.db() as connection:
            connection.execute(
                "INSERT INTO trades (time, symbol) VALUES ('t', 'BTC/USDT')")
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")     # closed, not just committed
        with sqlite3.connect(self.db_path) as check:
            self.assertEqual(
                check.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 1)


class TestAccessGuard(ServerTestCase):
    """Binding to 127.0.0.1 keeps the network out, not other browser tabs."""

    OWN_ORIGIN = "http://localhost"      # the test client's own host_url

    def test_a_mutating_request_without_the_token_is_refused(self):
        plain = server.app.test_client()
        response = plain.post("/api/config", json={"min_profit": 9.0})
        self.assertEqual(response.status_code, 403)
        self.assertIn("session token", response.get_json()["error"])
        self.assertNotEqual(server.state["config"]["min_profit"], 9.0)

    def test_every_mutating_route_is_covered(self):
        plain = server.app.test_client()
        posts = [
            "/api/start", "/api/pause", "/api/emergency-stop", "/api/reset",
            "/api/config", "/api/test-connection", "/api/recovery/close",
            "/api/risk/resume", "/api/risk/stranded/clear",
        ]
        for path in posts:
            with self.subTest(path=path):
                response = plain.post(path, json={})
                self.assertEqual(response.status_code, 403)
                # A rejection must not answer with the credential it was
                # rejected for lacking - otherwise the first refused request
                # hands the caller everything it needs for the second.
                self.assertNotIn("Set-Cookie", response.headers)

    def test_reading_state_does_not_need_the_token(self):
        plain = server.app.test_client()
        for path in ("/api/state", "/api/readiness"):
            with self.subTest(path=path):
                self.assertEqual(plain.get(path).status_code, 200)
        for path in ("/api/risk", "/api/recovery"):
            with self.subTest(path=path):
                self.assertEqual(plain.get(path).status_code, 403)

    def test_another_local_port_cannot_ride_the_cookie(self):
        # SameSite treats every port on localhost as the same site, so a second
        # dev server the operator happens to visit would be able to POST here
        # with the cookie attached. The origin comparison is what stops it.
        response = self.client().post(
            "/api/config", json={"min_profit": 9.0},
            headers={"Origin": "http://localhost:3000"})
        self.assertEqual(response.status_code, 403)
        self.assertIn("another page", response.get_json()["error"])
        self.assertNotEqual(server.state["config"]["min_profit"], 9.0)

    def test_a_cross_site_origin_is_refused_even_with_the_token(self):
        response = self.client().post(
            "/api/config", json={"min_profit": 9.0},
            headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        self.assertNotEqual(server.state["config"]["min_profit"], 9.0)

    def test_the_dashboard_origin_is_accepted(self):
        response = self.client().post(
            "/api/config", json={"min_profit": 0.31},
            headers={"Origin": self.OWN_ORIGIN})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(server.state["config"]["min_profit"], 0.31)

    def test_a_file_url_origin_is_refused(self):
        # "null" is what a browser sends for a page opened straight off disk.
        response = self.client().post("/api/config", json={"min_profit": 9.0},
                                      headers={"Origin": "null"})
        self.assertEqual(response.status_code, 403)

    def test_loading_the_dashboard_hands_out_the_cookie(self):
        plain = server.app.test_client()
        response = plain.get("/")
        self.addCleanup(response.close)      # send_from_directory holds the file
        self.assertEqual(response.status_code, 200)
        cookie = next(h for name, h in response.headers
                      if name == "Set-Cookie" and h.startswith(server.TOKEN_COOKIE))
        self.assertIn(server.API_TOKEN, cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        # The CSRF cookie is necessary but no longer sufficient: a browser must
        # also establish its own signed login session.
        self.assertEqual(
            plain.post("/api/config", json={"min_profit": 0.22},
                       headers={"Origin": self.OWN_ORIGIN}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
