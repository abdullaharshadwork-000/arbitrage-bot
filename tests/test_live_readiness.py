"""Regression tests for the dashboard-to-exchange real-order safety gates."""

import sys
import types
import unittest
from unittest import mock
from datetime import datetime, timedelta

import arbitrage_bot as bot
import server
from arbicore import config as arbiconfig


class SandboxClientTests(unittest.TestCase):
    def test_sandbox_is_enabled_before_any_exchange_request(self):
        events = []

        class FakeBinance:
            def __init__(self, config):
                events.append(("construct", bool(config.get("apiKey"))))

            def set_sandbox_mode(self, enabled):
                events.append(("sandbox", enabled))

        fake_ccxt = types.SimpleNamespace(binance=FakeBinance)
        with mock.patch.dict(sys.modules, {"ccxt": fake_ccxt}), \
             mock.patch.object(bot, "SANDBOX_MODE", True), \
             mock.patch.object(bot, "resolve_credentials") as credentials:
            credentials.return_value.ccxt_params.return_value = {}
            bot.create_exchange_client("binance")

        self.assertEqual(events, [("construct", False), ("sandbox", True)])

    def test_testnet_does_not_require_the_real_loss_acknowledgement(self):
        saved = {name: getattr(bot, name) for name in
                 ("REAL_TRADING_ENABLED", "REAL_TRADING_ACK", "SANDBOX_MODE",
                  "MODE", "EXECUTION_MODE", "TRADING_STRATEGY", "EXCHANGES")}
        try:
            bot.REAL_TRADING_ENABLED = True
            bot.REAL_TRADING_ACK = ""
            bot.SANDBOX_MODE = True
            bot.MODE = "live"
            bot.EXECUTION_MODE = "real"
            bot.TRADING_STRATEGY = "triangular"
            bot.EXCHANGES = ["binance"]
            with mock.patch.object(bot, "resolve_credentials") as credentials:
                credentials.return_value.complete = True
                credentials.return_value.api_key = "key"
                credentials.return_value.api_secret = "secret"
                result = bot.validate_real_trading_config()
            self.assertTrue(result["ok"])
        finally:
            for name, value in saved.items():
                setattr(bot, name, value)

    def test_live_order_has_a_client_id_for_timeout_recovery(self):
        class Client:
            def __init__(self):
                self.params = None

            def create_market_buy_order(self, symbol, quantity, params):
                self.params = params
                return {"id": "order-1"}

            def create_market_sell_order(self, symbol, quantity, params):
                raise AssertionError("wrong side")

        engine = object.__new__(bot.RealExecutionEngine)
        client = Client()
        engine.clients = {"binance": client}
        order = engine._place_market_order("binance", "BTC/USDT", "buy", 0.001)
        self.assertTrue(client.params["clientOrderId"].startswith("arbi"))
        self.assertEqual(order["clientOrderId"], client.params["clientOrderId"])

    def test_production_order_is_bounded_and_fill_or_kill(self):
        class Client:
            def __init__(self):
                self.request = None

            def price_to_precision(self, symbol, price):
                return str(price)

            def create_order(self, *args):
                self.request = args
                return {"id": "protected-1", "status": "closed"}

        engine = object.__new__(bot.RealExecutionEngine)
        client = Client()
        engine.clients = {"binance": client}
        engine.bounded_orders = True
        engine._approved_limit_prices = {
            ("binance", "BTC/USDT", "buy"): 100.25,
        }

        order = engine._place_market_order("binance", "BTC/USDT", "buy", 0.01)

        symbol, order_type, side, amount, price, params = client.request
        self.assertEqual((symbol, order_type, side), ("BTC/USDT", "limit", "buy"))
        self.assertEqual((amount, price), (0.01, 100.25))
        self.assertEqual(params["timeInForce"], "FOK")
        self.assertEqual(order["clientOrderId"], params["clientOrderId"])

    def test_binance_connection_uses_the_non_executing_test_order_endpoint(self):
        client = mock.Mock()
        client.fetch_ticker.return_value = {"ask": 100000.0}
        client.fetch_balance.return_value = {"USDT": {"free": 100.0}}
        client.fetch_trading_fee.return_value = {"taker": 0.001}
        client.amount_to_precision.side_effect = lambda symbol, amount: str(amount)
        engine = object.__new__(bot.RealExecutionEngine)
        engine.clients = {"binance": client}
        engine.markets = {"binance": {"BTC/USDT": {
            "limits": {"amount": {"min": 0.00001}, "cost": {"min": 5.0}},
            "precision": {"amount": 0.00001},
        }}}

        result = engine.connection_status("binance")

        self.assertTrue(result["ok"])
        self.assertTrue(result["trade_access"])
        args = client.create_order.call_args.args
        self.assertEqual(args[:3], ("BTC/USDT", "market", "buy"))
        self.assertEqual(args[-1], {"test": True})


class ServerRealStartTests(unittest.TestCase):
    def setUp(self):
        self.saved_user = server.state.get("current_user")
        self.saved_config = dict(server.state["config"])
        self.saved_verified = server.state.get("connection_verified_at")
        self.saved_verification = server.state.get("connection_verification")
        self.saved_engine = server.real_engine
        self.saved_running = server.state.get("running")
        self.saved_credentials = dict(bot.EXCHANGE_CREDENTIALS.get("binance") or {})
        server.state["current_user"] = {"username": "admin", "role": "admin"}
        server.state["config"].update({
            "mode": "live", "execution_mode": "real",
            "real_trading_enabled": True, "sandbox_mode": True,
        })
        server.state["connection_verified_at"] = None
        server.state["connection_verification"] = None
        server.real_engine = object()
        self.client = server.app.test_client()
        self.client.environ_base["HTTP_X_ARBICORE_TOKEN"] = server.API_TOKEN

    def tearDown(self):
        server.state["current_user"] = self.saved_user
        server.state["config"] = self.saved_config
        server.state["connection_verified_at"] = self.saved_verified
        server.state["connection_verification"] = self.saved_verification
        server.real_engine = self.saved_engine
        server.state["running"] = self.saved_running
        bot.EXCHANGE_CREDENTIALS["binance"] = self.saved_credentials

    def test_real_start_requires_a_successful_authenticated_connection_check(self):
        with mock.patch.object(bot, "validate_real_trading_config",
                               return_value={"ok": True, "message": "valid"}):
            response = self.client.post("/api/start")
        self.assertEqual(response.status_code, 409)
        self.assertIn("test authenticated", response.get_json()["error"].lower())

    def test_browser_credentials_are_memory_only_and_never_echo_the_secret(self):
        server.state["running"] = False
        empty = arbiconfig.Credentials("binance")
        with mock.patch.object(arbiconfig.Credentials, "from_env",
                               return_value=empty):
            response = self.client.post("/api/user/api-keys", json={
                "exchange": "binance", "api_key": "test-key-123456",
                "api_secret": "top-secret-value"})
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertNotIn("top-secret-value", str(payload))
        self.assertFalse(payload["persistent"])
        self.assertEqual(bot.EXCHANGE_CREDENTIALS["binance"]["secret"],
                         "top-secret-value")

    def test_testnet_verification_cannot_authorize_production(self):
        verified_at = datetime.now().isoformat()
        server.state["config"]["sandbox_mode"] = False
        server.state["connection_verification"] = {
            "verified_at": verified_at, "target": "sandbox",
            "exchanges": list(server.state["active_exchanges"]),
            "strategy": server.state["config"]["strategy"],
        }
        with mock.patch.object(bot, "validate_real_trading_config",
                               return_value={"ok": True, "message": "valid"}):
            readiness = server.get_readiness()
        self.assertFalse(readiness["ready"])

    def test_expired_connection_verification_cannot_start_real_orders(self):
        verified_at = (datetime.now() - timedelta(
            seconds=server.CONNECTION_VERIFICATION_TTL_SECONDS + 1)).isoformat()
        server.state["connection_verification"] = {
            "verified_at": verified_at, "target": "sandbox",
            "exchanges": list(server.state["active_exchanges"]),
            "strategy": server.state["config"]["strategy"],
        }
        with mock.patch.object(bot, "validate_real_trading_config",
                               return_value={"ok": True, "message": "valid"}):
            readiness = server.get_readiness()
        self.assertFalse(readiness["ready"])


if __name__ == "__main__":
    unittest.main()
