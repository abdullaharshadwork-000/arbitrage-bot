import os
import unittest
from unittest import mock
from unittest.mock import Mock

import arbitrage_bot
import server
from arbicore import config as arbiconfig


def exchange_mock():
    client = Mock()
    client.private_get_account_commission.return_value = {
        "standardCommission": {"taker": str(arbitrage_bot.TAKER_FEE)}}
    client.fetch_trading_fee.return_value = {"taker": arbitrage_bot.TAKER_FEE}
    return client


def api_client():
    """A test client carrying the session token the access guard requires.

    In a browser the dashboard picks the token up as a SameSite cookie on its
    first GET; a script has to send the header, and so does a test.
    """
    client = server.app.test_client()
    client.environ_base["HTTP_X_ARBICORE_TOKEN"] = server.API_TOKEN
    return client


class RealTradingConfigTests(unittest.TestCase):
    def test_demo_mode_ignores_live_config_override(self):
        original_mode = arbitrage_bot.MODE
        original_execution = arbitrage_bot.EXECUTION_MODE
        original_enabled = arbitrage_bot.REAL_TRADING_ENABLED
        original_exchanges = arbitrage_bot.EXCHANGES[:]
        original_credentials = arbitrage_bot.EXCHANGE_CREDENTIALS.copy()
        config_path = arbitrage_bot.Path(arbitrage_bot.__file__).with_name("live_config.py")
        original_contents = config_path.read_text() if config_path.exists() else None

        try:
            arbitrage_bot.MODE = "demo"
            arbitrage_bot.EXECUTION_MODE = "paper"
            arbitrage_bot.REAL_TRADING_ENABLED = False
            arbitrage_bot.EXCHANGES = ["binance", "kucoin"]
            arbitrage_bot.EXCHANGE_CREDENTIALS = {
                "binance": {"apiKey": "abc", "secret": "def"},
                "kucoin": {"apiKey": "ghi", "secret": "jkl"},
            }
            config_path.write_text(
                "REAL_TRADING_ENABLED = False\n"
                "MODE = 'live'\n"
                "EXECUTION_MODE = 'paper'\n"
                "TRADING_STRATEGY = 'cross_exchange'\n"
                "EXCHANGES = ['binance', 'kucoin']\n"
                "EXCHANGE_CREDENTIALS = {'binance': {'apiKey': 'X', 'secret': 'Y'}, 'kucoin': {'apiKey': 'A', 'secret': 'B'}}\n"
            )

            arbitrage_bot.load_live_config_if_present()

            self.assertEqual(arbitrage_bot.MODE, "demo")
            self.assertEqual(arbitrage_bot.EXECUTION_MODE, "paper")
        finally:
            if original_contents is None:
                config_path.unlink(missing_ok=True)
            else:
                config_path.write_text(original_contents)
            arbitrage_bot.MODE = original_mode
            arbitrage_bot.EXECUTION_MODE = original_execution
            arbitrage_bot.REAL_TRADING_ENABLED = original_enabled
            arbitrage_bot.EXCHANGES = original_exchanges
            arbitrage_bot.EXCHANGE_CREDENTIALS = original_credentials

    def test_real_trading_requires_explicit_enable(self):
        original = arbitrage_bot.REAL_TRADING_ENABLED
        original_keys = arbitrage_bot.EXCHANGE_CREDENTIALS.copy()
        try:
            arbitrage_bot.REAL_TRADING_ENABLED = False
            arbitrage_bot.EXCHANGE_CREDENTIALS = {
                "binance": {"apiKey": "abc", "secret": "def"},
                "kucoin": {"apiKey": "ghi", "secret": "jkl"},
            }
            result = arbitrage_bot.validate_real_trading_config()
            self.assertFalse(result["ok"])
            self.assertIn("disabled", result["message"].lower())
        finally:
            arbitrage_bot.REAL_TRADING_ENABLED = original
            arbitrage_bot.EXCHANGE_CREDENTIALS = original_keys

    def test_wallet_round_trip_uses_usdt(self):
        wallet = arbitrage_bot.PaperWallet(
            ["binance"],
            ["BTC/USDT"],
            1000,
            {"BTC/USDT": 10000},
        )
        bought = wallet.buy("binance", "BTC/USDT", 100, 10000, 0.001)
        self.assertIsNotNone(bought)
        self.assertGreater(bought, 0)

    def test_paper_wallet_starts_with_one_fixed_twenty_thousand_total(self):
        prices = {"BTC/USDT": 10000, "ETH/USDT": 1000}
        wallet = arbitrage_bot.PaperWallet.with_total_balance(
            ["binance", "kucoin"], list(prices), 20_000, prices)

        self.assertAlmostEqual(wallet.total_value(prices), 20_000.0)

    def test_paper_wallet_keeps_last_mark_when_a_live_quote_is_missing(self):
        prices = {"BTC/USDT": 10000, "ETH/USDT": 1000}
        wallet = arbitrage_bot.PaperWallet.with_total_balance(
            ["binance", "kucoin"], list(prices), 20_000, prices)

        partial_value = wallet.total_value({"BTC/USDT": 11000})

        self.assertGreater(partial_value, 20_000.0)
        self.assertAlmostEqual(wallet.total_value({}), partial_value)

    def test_triangular_opportunity_applies_three_fees(self):
        original_min_profit = arbitrage_bot.MIN_PROFIT_PCT
        try:
            arbitrage_bot.MIN_PROFIT_PCT = 0.1
            quotes = {
                "BTC/USDT": {"binance": {"bid": 10000, "ask": 10000}},
                "ETH/BTC": {"binance": {"bid": 0.05, "ask": 0.05}},
                "ETH/USDT": {"binance": {"bid": 510, "ask": 510}},
            }
            result = arbitrage_bot.find_triangular_opportunity(
                quotes, "binance", 100, 0.001)

            self.assertIsNotNone(result)
            self.assertEqual(result["route"], ["USDT", "BTC", "ETH", "USDT"])
            self.assertGreater(result["profit_usdt"], 0)
        finally:
            arbitrage_bot.MIN_PROFIT_PCT = original_min_profit

    def test_triangular_routes_are_discovered_from_market_symbols(self):
        routes = arbitrage_bot.discover_triangular_routes({
            "BTC/USDT": {},
            "ETH/BTC": {},
            "ETH/USDT": {},
            "SOL/USDT": {},
            "ETH/SOL": {},
        })

        route_symbols = {tuple(route["symbols"]) for route in routes}
        self.assertIn(("BTC/USDT", "ETH/BTC", "ETH/USDT"), route_symbols)
        self.assertIn(("SOL/USDT", "ETH/SOL", "ETH/USDT"), route_symbols)

    def test_discovered_triangular_route_calculates_profit(self):
        quotes = {
            "SOL/USDT": {"kucoin": {"bid": 99, "ask": 100}},
            "ETH/SOL": {"kucoin": {"bid": 0.5, "ask": 0.5}},
            "ETH/USDT": {"kucoin": {"bid": 210, "ask": 211}},
        }
        result = arbitrage_bot.calculate_triangular_route(
            quotes, "kucoin", 100, 0.001,
            ["SOL/USDT", "ETH/SOL", "ETH/USDT"],
            ["USDT", "SOL", "ETH", "USDT"],
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["route"], ["USDT", "SOL", "ETH", "USDT"])
        self.assertGreater(result["profit_usdt"], 0)

    def test_best_triangular_opportunity_selects_highest_profit_route(self):
        quotes = {
            "SOL/USDT": {"kucoin": {"bid": 99, "ask": 100}},
            "ETH/SOL": {"kucoin": {"bid": 0.5, "ask": 0.5}},
            "ETH/USDT": {"kucoin": {"bid": 210, "ask": 211}},
            "BTC/USDT": {"kucoin": {"bid": 9999, "ask": 10000}},
            "ETH/BTC": {"kucoin": {"bid": 0.05, "ask": 0.05}},
        }
        routes = arbitrage_bot.discover_triangular_routes(
            {symbol: {} for symbol in quotes})
        original_min_profit = arbitrage_bot.MIN_PROFIT_PCT
        try:
            arbitrage_bot.MIN_PROFIT_PCT = 0.01
            result = arbitrage_bot.find_best_triangular_opportunity(
                quotes, "kucoin", 100, 0.001, routes)
            self.assertIsNotNone(result)
            self.assertEqual(result["symbols"], ["SOL/USDT", "ETH/SOL", "ETH/USDT"])
        finally:
            arbitrage_bot.MIN_PROFIT_PCT = original_min_profit

    def test_triangular_strategy_allows_one_configured_exchange(self):
        originals = {
            "enabled": arbitrage_bot.REAL_TRADING_ENABLED,
            "mode": arbitrage_bot.MODE,
            "execution": arbitrage_bot.EXECUTION_MODE,
            "strategy": arbitrage_bot.TRADING_STRATEGY,
            "exchanges": arbitrage_bot.EXCHANGES,
            "credentials": arbitrage_bot.EXCHANGE_CREDENTIALS,
        }
        try:
            arbitrage_bot.REAL_TRADING_ENABLED = True
            arbitrage_bot.MODE = "live"
            arbitrage_bot.EXECUTION_MODE = "paper"
            arbitrage_bot.TRADING_STRATEGY = "triangular"
            arbitrage_bot.EXCHANGES = ["binance"]
            arbitrage_bot.EXCHANGE_CREDENTIALS = {
                "binance": {"apiKey": "abc", "secret": "def"},
            }
            result = arbitrage_bot.validate_real_trading_config()
            self.assertFalse(result["ok"])
            self.assertIn("execution_mode", result["message"].lower())
        finally:
            arbitrage_bot.REAL_TRADING_ENABLED = originals["enabled"]
            arbitrage_bot.MODE = originals["mode"]
            arbitrage_bot.EXECUTION_MODE = originals["execution"]
            arbitrage_bot.TRADING_STRATEGY = originals["strategy"]
            arbitrage_bot.EXCHANGES = originals["exchanges"]
            arbitrage_bot.EXCHANGE_CREDENTIALS = originals["credentials"]

    def test_triangular_strategy_accepts_single_kucoin_account(self):
        originals = (
            arbitrage_bot.REAL_TRADING_ENABLED,
            arbitrage_bot.MODE,
            arbitrage_bot.EXECUTION_MODE,
            arbitrage_bot.TRADING_STRATEGY,
            arbitrage_bot.EXCHANGES,
            arbitrage_bot.EXCHANGE_CREDENTIALS,
            arbitrage_bot.REAL_TRADING_ACK,
        )
        try:
            arbitrage_bot.REAL_TRADING_ENABLED = True
            arbitrage_bot.REAL_TRADING_ACK = arbiconfig.REAL_TRADING_ACK
            arbitrage_bot.MODE = "live"
            arbitrage_bot.EXECUTION_MODE = "real"
            arbitrage_bot.TRADING_STRATEGY = "triangular"
            arbitrage_bot.EXCHANGES = ["kucoin"]
            arbitrage_bot.EXCHANGE_CREDENTIALS = {
                "kucoin": {"apiKey": "abc", "secret": "def",
                            "password": "phrase"},
            }
            result = arbitrage_bot.validate_real_trading_config()
            self.assertTrue(result["ok"], result["message"])
        finally:
            (arbitrage_bot.REAL_TRADING_ENABLED,
             arbitrage_bot.MODE,
             arbitrage_bot.EXECUTION_MODE,
             arbitrage_bot.TRADING_STRATEGY,
             arbitrage_bot.EXCHANGES,
             arbitrage_bot.EXCHANGE_CREDENTIALS,
             arbitrage_bot.REAL_TRADING_ACK) = originals

    def test_atomic_arbitrage_does_not_buy_when_sell_balance_is_missing(self):
        wallet = arbitrage_bot.PaperWallet(
            ["binance", "kucoin"],
            ["BTC/USDT"],
            1000,
            {"BTC/USDT": 10000},
        )
        wallet.coin["kucoin"]["BTC/USDT"] = 0
        starting_usdt = wallet.usdt["binance"]["BTC/USDT"]
        starting_coin = wallet.coin["binance"]["BTC/USDT"]

        result = wallet.execute_arbitrage(
            "binance", "kucoin", "BTC/USDT", 100, 10000, 10100, 0.001)

        self.assertIsNone(result)
        self.assertEqual(wallet.usdt["binance"]["BTC/USDT"], starting_usdt)
        self.assertEqual(wallet.coin["binance"]["BTC/USDT"], starting_coin)

    def test_api_config_rejects_invalid_numeric_values(self):
        client = api_client()
        response = client.post("/api/config", json={"fee": -0.1})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])

    def test_api_config_rejects_invalid_execution_mode(self):
        client = api_client()
        response = client.post("/api/config", json={"execution_mode": "live-orders"})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])

    def test_readiness_reports_paper_mode(self):
        client = api_client()
        response = client.get("/api/readiness")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["ready"])
        self.assertIn("paper", response.get_json()["message"].lower())

    def test_history_endpoint_returns_persisted_collections(self):
        client = api_client()
        response = client.get("/api/history")

        self.assertEqual(response.status_code, 200)
        self.assertIn("trades", response.get_json())
        self.assertIn("recovery", response.get_json())

    def test_connection_status_reports_latency_and_balance_access(self):
        client = Mock()
        client.fetch_ticker.return_value = {"bid": 100, "ask": 101}
        client.fetch_balance.return_value = {"USDT": {"free": 100}}
        client.fetch_trading_fee.return_value = {"taker": 0.001}
        engine = arbitrage_bot.RealExecutionEngine.__new__(
            arbitrage_bot.RealExecutionEngine)
        engine.clients = {"kucoin": client}
        engine.markets = {"kucoin": {"BTC/USDT": {}}}

        result = engine.connection_status("kucoin")

        self.assertTrue(result["ok"])
        self.assertTrue(result["balance_access"])
        self.assertEqual(result["fee"], 0.001)
        self.assertIsNotNone(result["latency_ms"])

    def test_detected_taker_fee_overrides_configured_fallback(self):
        client = Mock()
        client.fetch_trading_fee.return_value = {"taker": 0.012}
        engine = arbitrage_bot.RealExecutionEngine.__new__(
            arbitrage_bot.RealExecutionEngine)
        engine.clients = {"kucoin": client}
        engine.fee_rates = {}

        fee = engine.taker_fee("kucoin")

        self.assertEqual(fee, 0.012)
        self.assertEqual(engine.taker_fee("kucoin"), 0.012)
        self.assertEqual(client.fetch_trading_fee.call_count, 1)

    def test_live_balances_are_valued_in_usdt(self):
        client = Mock()
        client.fetch_ticker.return_value = {"bid": 10000, "ask": 10010}
        engine = arbitrage_bot.RealExecutionEngine.__new__(
            arbitrage_bot.RealExecutionEngine)
        engine.clients = {"kucoin": client}
        engine.markets = {"kucoin": {"BTC/USDT": {}}}
        balances = {
            "USDT": {"free": 50.0, "used": 10.0, "total": 60.0},
            "BTC": {"free": 0.01, "used": 0.0, "total": 0.01},
        }

        result = engine.value_balances_usdt("kucoin", balances)

        self.assertAlmostEqual(result["free_usdt"], 150.0)
        self.assertAlmostEqual(result["total_usdt"], 160.0)

    def test_recovery_close_requires_explicit_confirmation(self):
        client = api_client()
        response = client.post("/api/recovery/close", json={})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])

    def test_recovery_close_requires_real_execution_mode(self):
        client = api_client()
        response = client.post(
            "/api/recovery/close",
            json={"confirmation": "CLOSE_UNHEDGED_POSITION", "buy_order_id": "missing"},
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.get_json()["ok"])

    def test_emergency_stop_blocks_restart_until_reset(self):
        client = api_client()
        stopped = client.post("/api/emergency-stop")
        blocked = client.post("/api/start")
        reset = client.post("/api/reset")

        self.assertEqual(stopped.status_code, 200)
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(reset.status_code, 200)

    def test_real_engine_submits_both_legs_after_balance_preflight(self):
        original_enabled = arbitrage_bot.REAL_TRADING_ENABLED
        original_mode = arbitrage_bot.EXECUTION_MODE
        try:
            arbitrage_bot.REAL_TRADING_ENABLED = True
            arbitrage_bot.EXECUTION_MODE = "real"
            buy_client = exchange_mock()
            sell_client = exchange_mock()
            balances = {"USDT": {"free": 1000}, "BTC": {"free": 1}}
            buy_client.fetch_balance.return_value = balances
            sell_client.fetch_balance.return_value = balances
            buy_client.fetch_ticker.return_value = {"ask": 10000}
            buy_client.fetch_order_book.return_value = {
                "asks": [[10000, 1]], "bids": [[10000, 1]],
            }
            sell_client.fetch_order_book.return_value = {
                "asks": [[10500, 1]], "bids": [[10500, 1]],
            }
            buy_client.create_market_buy_order.return_value = {
                "id": "buy-1", "filled": 0.001998, "cost": 20,
            }
            sell_client.create_market_sell_order.return_value = {
                "id": "sell-1", "filled": 0.001998, "cost": 21,
            }

            engine = arbitrage_bot.RealExecutionEngine.__new__(
                arbitrage_bot.RealExecutionEngine)
            engine.clients = {"binance": buy_client, "kucoin": sell_client}
            result = engine.execute_arbitrage(
                "binance", "kucoin", "BTC/USDT", 20, 10000, 10500)

            self.assertEqual(result["buy_order"]["id"], "buy-1")
            self.assertEqual(result["sell_order"]["id"], "sell-1")
            self.assertAlmostEqual(result["profit_usdt"], 1)
        finally:
            arbitrage_bot.REAL_TRADING_ENABLED = original_enabled
            arbitrage_bot.EXECUTION_MODE = original_mode

    def test_real_engine_surfaces_sell_failure(self):
        original_enabled = arbitrage_bot.REAL_TRADING_ENABLED
        original_mode = arbitrage_bot.EXECUTION_MODE
        try:
            arbitrage_bot.REAL_TRADING_ENABLED = True
            arbitrage_bot.EXECUTION_MODE = "real"
            buy_client = exchange_mock()
            sell_client = exchange_mock()
            balances = {"USDT": {"free": 1000}, "BTC": {"free": 1}}
            buy_client.fetch_balance.return_value = balances
            sell_client.fetch_balance.return_value = balances
            buy_client.fetch_ticker.return_value = {"ask": 10000}
            buy_client.fetch_order_book.return_value = {
                "asks": [[10000, 1]], "bids": [[10000, 1]],
            }
            sell_client.fetch_order_book.return_value = {
                "asks": [[10500, 1]], "bids": [[10500, 1]],
            }
            buy_client.create_market_buy_order.return_value = {
                "id": "buy-1", "filled": 0.001998, "cost": 20,
            }
            sell_client.create_market_sell_order.side_effect = RuntimeError("exchange rejected sell")

            engine = arbitrage_bot.RealExecutionEngine.__new__(
                arbitrage_bot.RealExecutionEngine)
            engine.clients = {"binance": buy_client, "kucoin": sell_client}

            with self.assertRaisesRegex(RuntimeError, "exchange rejected sell") as raised:
                engine.execute_arbitrage(
                    "binance", "kucoin", "BTC/USDT", 20, 10000, 10040)
            self.assertEqual(raised.exception.recovery_exchange, "binance")
        finally:
            arbitrage_bot.REAL_TRADING_ENABLED = original_enabled
            arbitrage_bot.EXECUTION_MODE = original_mode

    def test_real_triangular_engine_executes_three_legs(self):
        original_enabled = arbitrage_bot.REAL_TRADING_ENABLED
        original_mode = arbitrage_bot.EXECUTION_MODE
        try:
            arbitrage_bot.REAL_TRADING_ENABLED = True
            arbitrage_bot.EXECUTION_MODE = "real"
            client = exchange_mock()
            client.fetch_balance.return_value = {
                "USDT": {"free": 1000}, "BTC": {"free": 0}, "ETH": {"free": 0},
            }
            client.fetch_ticker.return_value = {"ask": 10000}
            client.fetch_order_book.side_effect = lambda symbol, limit=10: {
                "asks": [[10000 if symbol == "BTC/USDT" else 0.05, 10]],
                "bids": [[510 if symbol == "ETH/USDT" else 0.05, 10]],
            }
            # `status` matters now that every leg is reconciled against the
            # exchange: a triangular middle leg fills slightly under the
            # requested size, so without a terminal status the reconciler
            # correctly refuses to assume it is done and polls until it times
            # out. A real venue reports the status; the mock has to as well.
            client.create_market_buy_order.side_effect = [
                {"id": "btc-buy", "status": "closed", "filled": 0.001998, "cost": 20},
                {"id": "eth-buy", "status": "closed", "filled": 0.03992004,
                 "cost": 0.001996},
            ]
            client.create_market_sell_order.return_value = {
                "id": "eth-sell", "status": "closed", "filled": 0.03992004,
                "cost": 20.5,
            }

            engine = arbitrage_bot.RealExecutionEngine.__new__(
                arbitrage_bot.RealExecutionEngine)
            engine.clients = {"binance": client}
            result = engine.execute_triangular(
                "binance", 20, {
                    "BTC/USDT": 10000,
                    "ETH/BTC": 0.05,
                    "ETH/USDT": 510,
                })

            self.assertEqual(result["buy_order"]["id"], "btc-buy")
            self.assertEqual(result["middle_order"]["id"], "eth-buy")
            self.assertEqual(result["sell_order"]["id"], "eth-sell")
            self.assertAlmostEqual(result["profit_usdt"], 0.5)
        finally:
            arbitrage_bot.REAL_TRADING_ENABLED = original_enabled
            arbitrage_bot.EXECUTION_MODE = original_mode

    def test_real_engine_rejects_price_move_before_orders(self):
        original_enabled = arbitrage_bot.REAL_TRADING_ENABLED
        original_mode = arbitrage_bot.EXECUTION_MODE
        try:
            arbitrage_bot.REAL_TRADING_ENABLED = True
            arbitrage_bot.EXECUTION_MODE = "real"
            buy_client = exchange_mock()
            sell_client = exchange_mock()
            balances = {"USDT": {"free": 1000}, "BTC": {"free": 1}}
            buy_client.fetch_balance.return_value = balances
            sell_client.fetch_balance.return_value = balances
            buy_client.fetch_order_book.return_value = {
                "asks": [[10100, 1]], "bids": [[10000, 1]],
            }
            sell_client.fetch_order_book.return_value = {
                "asks": [[10500, 1]], "bids": [[10500, 1]],
            }

            engine = arbitrage_bot.RealExecutionEngine.__new__(
                arbitrage_bot.RealExecutionEngine)
            engine.clients = {"binance": buy_client, "kucoin": sell_client}

            with self.assertRaisesRegex(RuntimeError, "moved beyond"):
                engine.execute_arbitrage(
                    "binance", "kucoin", "BTC/USDT", 20, 10000, 10500)
            buy_client.create_market_buy_order.assert_not_called()
        finally:
            arbitrage_bot.REAL_TRADING_ENABLED = original_enabled
            arbitrage_bot.EXECUTION_MODE = original_mode

    def test_real_engine_rejects_fresh_quote_below_profit_floor(self):
        original_enabled = arbitrage_bot.REAL_TRADING_ENABLED
        original_mode = arbitrage_bot.EXECUTION_MODE
        original_min_profit = arbitrage_bot.MIN_PROFIT_PCT
        try:
            arbitrage_bot.REAL_TRADING_ENABLED = True
            arbitrage_bot.EXECUTION_MODE = "real"
            arbitrage_bot.MIN_PROFIT_PCT = 0.5
            buy_client = exchange_mock()
            sell_client = exchange_mock()
            balances = {"USDT": {"free": 1000}, "BTC": {"free": 1}}
            buy_client.fetch_balance.return_value = balances
            sell_client.fetch_balance.return_value = balances
            buy_client.fetch_ticker.return_value = {"ask": 10000}
            buy_client.fetch_order_book.return_value = {
                "asks": [[10000, 1]], "bids": [[10000, 1]],
            }
            sell_client.fetch_order_book.return_value = {
                "asks": [[10040, 1]], "bids": [[10040, 1]],
            }

            engine = arbitrage_bot.RealExecutionEngine.__new__(
                arbitrage_bot.RealExecutionEngine)
            engine.clients = {"binance": buy_client, "kucoin": sell_client}

            with self.assertRaisesRegex(RuntimeError, "below the"):
                engine.execute_arbitrage(
                    "binance", "kucoin", "BTC/USDT", 20, 10000, 10040)
            buy_client.create_market_buy_order.assert_not_called()
        finally:
            arbitrage_bot.REAL_TRADING_ENABLED = original_enabled
            arbitrage_bot.EXECUTION_MODE = original_mode
            arbitrage_bot.MIN_PROFIT_PCT = original_min_profit

    def test_api_config_updates_engine_state(self):
        """The settings reach the module and the published state.

        No engine is rebuilt: doing so would make this test depend on four
        exchanges being reachable. It also has to put the configuration back
        afterwards - leaving mode="live" behind made every later test that
        resets the engine dial out to the network, which is where 40 of this
        file's 48 seconds used to go.
        """
        before = dict(server.state["config"])
        before_exchanges = list(server.state["active_exchanges"])
        before_symbols = list(server.state["active_symbols"])
        originals = {name: getattr(arbitrage_bot, name)
                     for name in ("MODE", "TRADE_SIZE_USDT", "TAKER_FEE",
                                  "MIN_PROFIT_PCT", "CHECK_INTERVAL",
                                  "EXCHANGES", "SYMBOLS")}

        def restore():
            with server.state_lock:
                server.state["config"].update(before)
                server.state["active_exchanges"] = before_exchanges
                server.state["active_symbols"] = before_symbols
            for name, value in originals.items():
                setattr(arbitrage_bot, name, value)

        self.addCleanup(restore)

        client = api_client()
        with mock.patch.object(server, "init_engine"):
            response = client.post(
                "/api/config",
                json={
                    "mode": "live",
                    "trade_size": 333,
                    "fee": 0.002,
                    "min_profit": 0.5,
                    "interval": 4,
                    "exchanges": ["binance", "okx"],
                    "symbols": ["BTC/USDT", "ETH/USDT"],
                },
            )
            self.assertEqual(response.status_code, 200)

            state = client.get("/api/state").get_json()
        self.assertEqual(state["config"]["mode"], "live")
        self.assertEqual(state["config"]["trade_size"], 333)
        self.assertEqual(state["config"]["fee"], 0.002)
        self.assertEqual(state["config"]["min_profit"], 0.5)
        self.assertEqual(state["config"]["interval"], 4)

        self.assertEqual(arbitrage_bot.MODE, "live")
        self.assertEqual(arbitrage_bot.TRADE_SIZE_USDT, 333)
        self.assertEqual(arbitrage_bot.TAKER_FEE, 0.002)
        self.assertEqual(arbitrage_bot.MIN_PROFIT_PCT, 0.5)
        self.assertEqual(arbitrage_bot.CHECK_INTERVAL, 4)


class CredentialResolutionTests(unittest.TestCase):
    """Where the keys come from, and what counts as not having any."""

    def setUp(self):
        self.original = dict(arbitrage_bot.EXCHANGE_CREDENTIALS)
        self.addCleanup(setattr, arbitrage_bot, "EXCHANGE_CREDENTIALS",
                        self.original)

    def test_the_environment_beats_the_config_file(self):
        arbitrage_bot.EXCHANGE_CREDENTIALS = {
            "binance": {"apiKey": "from-file", "secret": "file-secret"},
        }
        with mock.patch.dict(os.environ, {
                "ARBI_BINANCE_API_KEY": "from-env",
                "ARBI_BINANCE_API_SECRET": "env-secret"}, clear=False):
            creds = arbitrage_bot.resolve_credentials("binance")

        self.assertEqual(creds.api_key, "from-env")
        self.assertEqual(creds.source, "env")

    def test_the_config_file_is_the_fallback(self):
        arbitrage_bot.EXCHANGE_CREDENTIALS = {
            "kucoin": {"apiKey": "k", "secret": "s", "password": "p"},
        }
        with mock.patch.dict(os.environ, {}, clear=True):
            creds = arbitrage_bot.resolve_credentials("kucoin")

        self.assertTrue(creds.complete)
        self.assertEqual(creds.password, "p")
        self.assertEqual(creds.source, "live_config")

    def test_a_secret_never_appears_in_a_repr(self):
        arbitrage_bot.EXCHANGE_CREDENTIALS = {
            "binance": {"apiKey": "AKIAsecretkey", "secret": "topsecret"},
        }
        with mock.patch.dict(os.environ, {}, clear=True):
            rendered = repr(arbitrage_bot.resolve_credentials("binance"))

        self.assertNotIn("topsecret", rendered)
        self.assertNotIn("AKIAsecretkey", rendered)

    def test_example_placeholders_count_as_no_credentials(self):
        """A copied live_config.example.py used to validate as fully configured,
        so the refusal came from the exchange mid-trade instead of at startup."""
        arbitrage_bot.EXCHANGE_CREDENTIALS = {
            "binance": {"apiKey": "PASTE_YOUR_BINANCE_API_KEY",
                        "secret": "PASTE_YOUR_BINANCE_SECRET"},
        }
        with mock.patch.dict(os.environ, {}, clear=True):
            creds = arbitrage_bot.resolve_credentials("binance")

        self.assertFalse(creds.complete)
        self.assertEqual(creds.missing(), ["api_key", "api_secret"])

    def test_placeholder_keys_do_not_pass_the_live_startup_check(self):
        saved = {name: getattr(arbitrage_bot, name) for name in
                 ("REAL_TRADING_ENABLED", "REAL_TRADING_ACK", "MODE",
                  "EXECUTION_MODE", "TRADING_STRATEGY", "EXCHANGES")}
        for name, value in saved.items():
            self.addCleanup(setattr, arbitrage_bot, name, value)

        arbitrage_bot.REAL_TRADING_ENABLED = True
        arbitrage_bot.REAL_TRADING_ACK = arbiconfig.REAL_TRADING_ACK
        arbitrage_bot.MODE = "live"
        arbitrage_bot.EXECUTION_MODE = "real"
        arbitrage_bot.TRADING_STRATEGY = "cross_exchange"
        arbitrage_bot.EXCHANGES = ["binance", "kucoin"]
        arbitrage_bot.EXCHANGE_CREDENTIALS = {
            "binance": {"apiKey": "PASTE_YOUR_BINANCE_API_KEY",
                        "secret": "PASTE_YOUR_BINANCE_SECRET"},
            "kucoin": {"apiKey": "PASTE_YOUR_KUCOIN_API_KEY",
                       "secret": "PASTE_YOUR_KUCOIN_SECRET"},
        }
        with mock.patch.dict(os.environ, {}, clear=True):
            result = arbitrage_bot.validate_real_trading_config()

        self.assertFalse(result["ok"])
        self.assertIn("API keys", result["message"])


class RealTradingAcknowledgementTests(unittest.TestCase):
    """The flag alone must not be enough to place an order."""

    def setUp(self):
        for name in ("REAL_TRADING_ENABLED", "REAL_TRADING_ACK", "MODE",
                     "EXECUTION_MODE", "TRADING_STRATEGY", "EXCHANGES",
                     "EXCHANGE_CREDENTIALS"):
            self.addCleanup(setattr, arbitrage_bot, name,
                            getattr(arbitrage_bot, name))
        arbitrage_bot.REAL_TRADING_ENABLED = True
        arbitrage_bot.MODE = "live"
        arbitrage_bot.EXECUTION_MODE = "real"
        arbitrage_bot.TRADING_STRATEGY = "cross_exchange"
        arbitrage_bot.EXCHANGES = ["binance", "kucoin"]
        arbitrage_bot.EXCHANGE_CREDENTIALS = {
            "binance": {"apiKey": "abc", "secret": "def"},
            "kucoin": {"apiKey": "ghi", "secret": "jkl",
                        "password": "phrase"},
        }

    def test_the_flag_without_the_acknowledgement_is_refused(self):
        arbitrage_bot.REAL_TRADING_ACK = ""
        with mock.patch.dict(os.environ, {}, clear=True):
            result = arbitrage_bot.validate_real_trading_config()

        self.assertFalse(result["ok"])
        self.assertIn("REAL_TRADING_ACK", result["message"])

    def test_a_near_miss_acknowledgement_is_refused(self):
        arbitrage_bot.REAL_TRADING_ACK = "i accept real losses"
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(
                arbitrage_bot.validate_real_trading_config()["ok"])

    def test_the_exact_acknowledgement_passes(self):
        arbitrage_bot.REAL_TRADING_ACK = arbiconfig.REAL_TRADING_ACK
        with mock.patch.dict(os.environ, {}, clear=True):
            result = arbitrage_bot.validate_real_trading_config()

        self.assertTrue(result["ok"], result["message"])

    def test_the_environment_can_supply_the_acknowledgement(self):
        arbitrage_bot.REAL_TRADING_ACK = ""
        with mock.patch.dict(
                os.environ,
                {"ARBI_REAL_TRADING_ACK": arbiconfig.REAL_TRADING_ACK},
                clear=True):
            result = arbitrage_bot.validate_real_trading_config()

        self.assertTrue(result["ok"], result["message"])

    def test_the_server_refuses_to_build_a_real_engine_without_it(self):
        """init_engine is the chokepoint: no acknowledgement, no engine."""
        arbitrage_bot.REAL_TRADING_ACK = ""
        before = dict(server.state["config"])
        self.addCleanup(server.state["config"].update, before)
        server.state["config"].update({
            "mode": "live", "execution_mode": "real",
            "real_trading_enabled": True,
        })
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError) as caught:
                with server.state_lock:
                    server.init_engine()

        self.assertIn("REAL_TRADING_ACK", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
