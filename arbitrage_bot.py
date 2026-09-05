#!/usr/bin/env python3
"""
=====================================================================
 CROSS-EXCHANGE ARBITRAGE BOT  —  PAPER TRADING EDITION
=====================================================================
What it does:
  1. Watches the price of crypto pairs (e.g. BTC/USDT) on several
     exchanges at the same time.
  2. Looks for a gap: coin is CHEAPER on exchange A and MORE
     EXPENSIVE on exchange B at the same moment.
  3. If the gap is bigger than the trading fees, it simulates:
        BUY on the cheap exchange  +  SELL on the expensive one
  4. Tracks your virtual profit/loss. NO real money is used.

Two modes:
  MODE = "demo"  -> fake prices generated locally (works offline,
                    good for testing and learning)
  MODE = "live"  -> real live prices from the exchanges (needs
                    internet, still paper trading - no real trades)

HOW TO RUN (see README.md for full instructions):
  pip install ccxt
  python3 arbitrage_bot.py
=====================================================================
"""

import csv
import importlib.util
import inspect
import os
import random
import time
from concurrent import futures
from datetime import datetime
from pathlib import Path

# arbicore holds the parts that decide whether real money moves: order-book
# walking, fill reconciliation and exact decimal money maths. They live in a
# package with their own tests because a rounding error here is a loss, not a
# cosmetic bug.
from arbicore import books, config as arbiconfig, feed, orders, streaming
from arbicore.money import D, HUNDRED, ONE, ZERO, net_spread_pct

# ==================================================================
#  CONFIGURATION  —  everything you might want to change is here
# ==================================================================

MODE = "demo"            # "demo" = fake prices | "live" = real prices
EXECUTION_MODE = "paper" # "paper" = simulated orders | "real" = live orders
TRADING_STRATEGY = "cross_exchange"  # or "triangular"

# IMPORTANT: real trading is disabled by default to stop accidental orders.
REAL_TRADING_ENABLED = False
# The second half of that gate. A boolean in a config file is one character away
# from True, and files get copied between machines with their flags intact - so
# real orders also need this typed out exactly, either here, in live_config.py,
# or as ARBI_REAL_TRADING_ACK in the environment. See arbicore.config.
REAL_TRADING_ACK = ""
# Route authenticated exchange calls to the venue's sandbox/testnet.  This is
# intentionally process configuration (not a dashboard-only flag): the client
# must be pointed at the sandbox before markets or balances are loaded.
SANDBOX_MODE = False
EXCHANGE_CREDENTIALS = {
    "binance": {"apiKey": "", "secret": ""},
    "kucoin": {"apiKey": "", "secret": ""},
    "okx": {"apiKey": "", "secret": ""},
    "bybit": {"apiKey": "", "secret": ""},
}
ORDER_INTENT_HOOK = None
ORDER_RESULT_HOOK = None

EXCHANGES = ["binance", "kucoin", "okx", "bybit"]   # live mode only
EXCHANGES_MASTER = list(EXCHANGES)

SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT"]
SYMBOLS_MASTER = list(SYMBOLS)

START_CASH_PER_EXCHANGE = 1000.0   # virtual USDT on each exchange,
                                   # per symbol (half is auto-converted
                                   # to the coin so we can sell instantly)

TRADE_SIZE_USDT = 200.0   # how much virtual money each trade uses

TAKER_FEE = 0.001         # 0.1% fee per trade (typical). You pay it
                          # TWICE in arbitrage: once buying, once selling.

MIN_PROFIT_PCT = 0.15     # only "trade" if expected profit AFTER fees
                          # is at least this many percent

MAX_SLIPPAGE_PCT = 0.25   # reject live orders if the book moves beyond this

ORDER_BOOK_DEPTH = 20     # levels to walk when pricing an order. The check is
                          # a size-weighted average over these levels, not the
                          # best price, so it needs real depth to be honest.

MAX_QUOTE_AGE_MS = 10000  # discard prices the exchange stamped older than
                          # this. A stale spread has already closed, so acting
                          # on one is a loss rather than a missed gain.

MAX_TRIANGULAR_ROUTES = 60  # routes kept after ranking by their thinnest leg's
                            # volume. Full discovery finds thousands, and
                            # pricing them all takes minutes per scan.

# ---- risk limits -------------------------------------------------------
# These latch: once a limit trips, the loop stops and a human has to look at
# it. That is the point. An arbitrage bot that keeps trading through a losing
# streak is not arbitraging, it is paying fees to discover that its prices are
# wrong.
MIN_NOTIONAL_USDT = 10.0          # below this most venues reject the order
MAX_DAILY_LOSS_USDT = 50.0        # realized loss that stops trading for the day
MAX_POSITION_NOTIONAL_USDT = 400.0  # largest single trade allowed
MAX_CONSECUTIVE_FAILURES = 3      # failures in a row that mean "stop guessing"
MAX_ORDERS_PER_MINUTE = 20        # rate cap, so a feed bug cannot machine-gun
MAX_CLOCK_SKEW_MS = 2000          # past this, signed requests get rejected and
                                  # "freshness" checks stop meaning anything
ALERT_WEBHOOK = ""                # Slack/Discord/generic URL, or empty for
                                  # console-only alerts

CHECK_INTERVAL = 5        # seconds between scans

LOG_FILE = "trades.csv"   # every simulated trade is saved here

# Demo mode only: how jumpy the fake market is
DEMO_GAP_CHANCE = 0.25    # chance per scan that a price gap opens
                          # (real markets gap far less often!)


def resolve_real_trading_ack(environ=None):
    """The acknowledgement as configured: environment first, then this module."""
    env = environ if environ is not None else os.environ
    return (env.get("ARBI_REAL_TRADING_ACK") or REAL_TRADING_ACK or "").strip()


def resolve_sandbox_mode(environ=None):
    """Whether authenticated clients target exchange test infrastructure."""
    env = environ if environ is not None else os.environ
    value = env.get("ARBI_SANDBOX_MODE", SANDBOX_MODE)
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def validate_real_trading_config():
    """Returns a dict describing whether it is safe to start live trading."""
    if not REAL_TRADING_ENABLED:
        return {
            "ok": False,
            "message": "Real trading is disabled. Set REAL_TRADING_ENABLED = True and add API keys before live trading.",
        }

    if MODE != "live":
        return {
            "ok": False,
            "message": "Mode must be 'live' when real trading is enabled.",
        }
    if EXECUTION_MODE != "real":
        return {
            "ok": False,
            "message": "EXECUTION_MODE must be 'real' when real trading is enabled.",
        }
    if not resolve_sandbox_mode() and resolve_real_trading_ack() != arbiconfig.REAL_TRADING_ACK:
        return {
            "ok": False,
            "message": (
                "Real trading needs the written acknowledgement as well as the "
                f"flag: set REAL_TRADING_ACK = {arbiconfig.REAL_TRADING_ACK!r} "
                "in live_config.py, or ARBI_REAL_TRADING_ACK in the "
                "environment. Two independent gates exist so that a config file "
                "copied from another machine cannot start placing orders on "
                "this one."
            ),
        }

    if TRADE_SIZE_USDT <= 0:
        return {"ok": False, "message": "TRADE_SIZE_USDT must be greater than zero."}
    if TAKER_FEE <= 0 or TAKER_FEE >= 0.05:
        return {"ok": False, "message": "TAKER_FEE must be positive and realistic (for example 0.001)."}
    if MIN_PROFIT_PCT <= 0:
        return {"ok": False, "message": "MIN_PROFIT_PCT must be positive."}
    if CHECK_INTERVAL <= 0:
        return {"ok": False, "message": "CHECK_INTERVAL must be greater than zero."}
    if MAX_SLIPPAGE_PCT <= 0 or MAX_SLIPPAGE_PCT > 5:
        return {"ok": False, "message": "MAX_SLIPPAGE_PCT must be between 0 and 5."}

    if TRADING_STRATEGY not in ("cross_exchange", "triangular"):
        return {"ok": False, "message": "TRADING_STRATEGY must be cross_exchange or triangular."}

    configured = []
    for exchange in EXCHANGES:
        creds = resolve_credentials(exchange)
        if creds.complete:
            configured.append(exchange)
        elif creds.api_key or creds.api_secret:
            return {
                "ok": False,
                "message": f"Exchange '{exchange}' is missing a full API credential pair.",
            }

    required_exchanges = 2 if TRADING_STRATEGY == "cross_exchange" else 1
    if len(configured) < required_exchanges:
        return {
            "ok": False,
            "message": (
                "At least two exchanges need valid API keys for cross-exchange arbitrage."
                if required_exchanges == 2
                else "At least one exchange needs valid API keys for triangular arbitrage."
            ),
        }

    return {"ok": True, "message": "Real trading configuration is valid."}


def load_live_config_if_present():
    """Loads a local live_config.py file only when it is explicitly intended for live/real trading.

    Demo mode must stay demo even if a local live_config.py exists on disk; otherwise the
    bot silently switches into live mode and appears to do nothing when there are no real
    credentials or no market data. The user must explicitly enable real trading or set the
    live-mode flag to opt in.
    """
    config_path = Path(__file__).with_name("live_config.py")
    if not config_path.exists():
        return False

    try:
        spec = importlib.util.spec_from_file_location("live_config", config_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        file_enabled = bool(getattr(module, "REAL_TRADING_ENABLED", False))
        file_mode = getattr(module, "MODE", MODE)
        file_execution_mode = getattr(module, "EXECUTION_MODE", EXECUTION_MODE)

        if MODE == "demo" and not file_enabled:
            # Stay in demo mode unless the file explicitly opts in to live/real trading.
            return False

        for key in ("REAL_TRADING_ENABLED", "REAL_TRADING_ACK", "SANDBOX_MODE", "MODE",
                    "EXECUTION_MODE", "TRADING_STRATEGY", "EXCHANGES",
                    "SYMBOLS", "EXCHANGE_CREDENTIALS"):
            if hasattr(module, key):
                globals()[key] = getattr(module, key)

        for key in ("TRADE_SIZE_USDT", "TAKER_FEE", "MIN_PROFIT_PCT", "CHECK_INTERVAL",
                    "MAX_SLIPPAGE_PCT", "ORDER_BOOK_DEPTH", "MAX_QUOTE_AGE_MS",
                    "MAX_TRIANGULAR_ROUTES", "MIN_NOTIONAL_USDT",
                    "MAX_DAILY_LOSS_USDT", "MAX_POSITION_NOTIONAL_USDT",
                    "MAX_CONSECUTIVE_FAILURES", "MAX_ORDERS_PER_MINUTE",
                    "ALERT_WEBHOOK"):
            if hasattr(module, key):
                globals()[key] = getattr(module, key)

        # These are the whitelists the web API validates operator input
        # against. Assigning them as locals (the original bug) left the API
        # unable to select any exchange the live config had just added.
        globals()["EXCHANGES_MASTER"] = list(EXCHANGES)
        globals()["SYMBOLS_MASTER"] = list(SYMBOLS)

        # Placeholder credentials are filtered where they are read, by
        # resolve_credentials(), rather than by rewriting EXCHANGE_CREDENTIALS
        # here - the old rewrite only matched a key equal to "PASTE_" and so
        # never fired for the example file's PASTE_YOUR_..._API_KEY values.

        return True
    except Exception as exc:
        print(f"[!] Could not load live config: {exc}")
        return False


CREDENTIAL_PLACEHOLDER_PREFIX = "PASTE_"


def _filled_in(value):
    """The credential as the operator meant it, or "" if they never set it."""
    text = str(value or "").strip()
    if not text or text.startswith(CREDENTIAL_PLACEHOLDER_PREFIX):
        return ""
    return text


def resolve_credentials(exchange_name, environ=None):
    """Keys for one exchange: environment first, then live_config.py.

    The environment wins because a key there is not sitting in a plaintext file
    on disk, not in an editor backup, and not in something that can be
    committed by accident. `arbicore.config.Credentials` owns the variable
    naming (ARBI_BINANCE_API_KEY, ARBI_BINANCE_API_SECRET,
    ARBI_BINANCE_PASSWORD) and the redacted repr, so there is one
    implementation rather than two that can drift apart.

    Placeholders from live_config.example.py count as absent. The previous check
    compared a key against the literal string "PASTE_", which never equals
    "PASTE_YOUR_BINANCE_API_KEY" - so a copied example passed the startup
    validation as fully configured, and the refusal arrived from the exchange in
    the middle of a trade rather than before the loop started.
    """
    from arbicore.config import Credentials

    from_env = Credentials.from_env(exchange_name, environ)
    if from_env.complete:
        return from_env

    supplied = EXCHANGE_CREDENTIALS.get(exchange_name) or {}
    return Credentials.from_mapping(exchange_name, {
        "apiKey": _filled_in(supplied.get("apiKey") or supplied.get("api_key")),
        "secret": _filled_in(supplied.get("secret") or supplied.get("api_secret")),
        "password": _filled_in(supplied.get("password")
                               or supplied.get("passphrase")),
    }, source="live_config")


def create_exchange_client(exchange_name):
    """Build a CCXT client using configured API credentials."""
    import ccxt

    creds = resolve_credentials(exchange_name)
    config = {"enableRateLimit": True}
    if exchange_name == "binance":
        # Binance currency metadata is an authenticated SAPI request in CCXT.
        # Market discovery must remain public so an auth/IP rejection can be
        # reported at the correct diagnostic stage instead of masquerading as
        # "could not load markets".
        config["options"] = {"fetchCurrencies": False}
    # Blank values are left out rather than passed as empty strings: some venues
    # treat a present-but-empty apiKey as a broken key instead of no key.
    config.update({name: value
                   for name, value in creds.ccxt_params().items() if value})

    exchange_class = getattr(ccxt, exchange_name, None)
    if exchange_class is None:
        raise ValueError(f"Unsupported exchange: {exchange_name}")
    client = exchange_class(config)
    sandbox_enabled = resolve_sandbox_mode()
    if sandbox_enabled:
        if not hasattr(client, "set_sandbox_mode"):
            raise ValueError(f"{exchange_name} does not expose sandbox mode through CCXT.")
        try:
            # CCXT requires this to be the first call after construction.
            client.set_sandbox_mode(True)
        except Exception as exc:
            raise RuntimeError(
                f"Could not enable sandbox mode for {exchange_name}: {exc}") from exc
    return client


class UnhedgedPositionError(RuntimeError):
    """Raised when a filled buy cannot be paired with its sell order.

    `quantity_confirmed` is False when the exchange never told us how much
    filled. The quantity is then an upper bound taken from the request, and no
    automated unwind may use it - a market sell of a size that was never
    bought either fails or dumps unrelated inventory.
    """

    def __init__(self, buy_exchange, sell_exchange, symbol, quantity,
                 buy_order, cause, recovery_exchange=None,
                 quantity_confirmed=True):
        self.buy_exchange = buy_exchange
        self.sell_exchange = sell_exchange
        self.symbol = symbol
        self.quantity = quantity
        self.buy_order = buy_order
        self.cause = cause
        self.recovery_exchange = recovery_exchange or buy_exchange
        self.quantity_confirmed = bool(quantity_confirmed)
        order_id = buy_order.get("id") or "unknown"
        qualifier = "" if self.quantity_confirmed else (
            " Fill size is UNCONFIRMED, so the quantity is an upper bound only."
        )
        super().__init__(
            f"Sell failed after buy order {order_id} filled: {cause}. "
            f"Manual recovery required.{qualifier}"
        )


class RealExecutionEngine:
    """Thin wrapper around live exchange APIs with strict safety checks."""

    def __init__(self, exchanges):
        self.exchanges = exchanges
        self.clients = {name: create_exchange_client(name) for name in exchanges}
        self.markets = {}
        self.fee_rates = {}
        # Production orders are bounded limit-FOK orders.  A market order can
        # run beyond the depth/slippage price approved moments earlier.
        self.bounded_orders = True
        self._approved_limit_prices = {}
        for name, client in self.clients.items():
            try:
                self.markets[name] = client.load_markets()
            except Exception as exc:
                raise RuntimeError(f"Could not load markets for {name}: {exc}") from exc

    def triangular_routes(self, exchange_name):
        return discover_triangular_routes(self.markets.get(exchange_name, {}))

    def taker_fee(self, exchange_name, symbol="BTC/USDT"):
        """Return the exchange taker fee, falling back to configured safety settings."""
        fee_rates = getattr(self, "fee_rates", {})
        if exchange_name in fee_rates:
            return fee_rates[exchange_name]
        fee = None
        client = self.clients[exchange_name]
        try:
            if hasattr(client, "fetch_trading_fee"):
                payload = client.fetch_trading_fee(symbol)
                value = payload.get("taker") if isinstance(payload, dict) else None
                if value is not None:
                    fee = float(value)
        except Exception:
            fee = None
        fee_rates[exchange_name] = fee if fee is not None else TAKER_FEE
        self.fee_rates = fee_rates
        return fee_rates[exchange_name]

    def connection_status(self, exchange_name):
        """Check public data, account auth, and trade permission in distinct stages."""
        client = self.clients[exchange_name]
        started = time.perf_counter()
        market_count = len(self.markets.get(exchange_name, {}))
        result = {
            "exchange": exchange_name,
            "ok": False,
            "market_count": market_count,
            "latency_ms": None,
            "fee": None,
            "balance_access": False,
            "trade_access": None,
            "stage": "public_market",
            "error": None,
            "sandbox": bool(getattr(client, "sandboxMode", False)),
        }
        try:
            ticker = client.fetch_ticker("BTC/USDT")
        except Exception as exc:
            result["error"] = f"Public market access failed: {exc}"
            result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
            return result

        result["stage"] = "account_authentication"
        try:
            client.fetch_balance()
            # A valid empty account response is still successful authentication.
            result["balance_access"] = True
        except Exception as exc:
            result["error"] = f"Account authentication failed: {exc}"
            result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
            return result

        result["stage"] = "trade_permission"
        try:
            if exchange_name == "binance":
                # CCXT's `test` flag routes Binance Spot to POST /api/v3/order/test.
                # Binance validates signature, TRADE permission, symbol filters,
                # and the proposed order but never sends it to the matching engine.
                ask = float(ticker.get("ask") or ticker.get("last") or 0.0)
                if ask <= 0:
                    raise RuntimeError("No usable BTC/USDT ask for the test order.")
                constraints = self.market_constraints(exchange_name, "BTC/USDT")
                test_cost = max(float(TRADE_SIZE_USDT), float(constraints["min_cost"] or 0.0))
                test_amount = max(test_cost / ask,
                                  float(constraints["min_amount"] or 0.0))
                test_amount = self.normalize_amount(
                    exchange_name, "BTC/USDT", test_amount)
                client.create_order("BTC/USDT", "market", "buy", test_amount,
                                    None, {"test": True})
                result["trade_access"] = True
        except Exception as exc:
            result["trade_access"] = False
            result["error"] = f"Trade-permission test failed: {exc}"
            result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
            return result

        result["stage"] = "complete"
        try:
            if hasattr(client, "fetch_trading_fee"):
                fee = client.fetch_trading_fee("BTC/USDT")
                taker = fee.get("taker") if isinstance(fee, dict) else None
                result["fee"] = float(taker) if taker is not None else None
        except Exception:
            # Fee lookup is advisory; order-test authorization is authoritative.
            result["fee"] = None
        result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        result["ok"] = (result["balance_access"] and market_count > 0
                        and result["trade_access"] is not False)
        return result

    def market_constraints(self, exchange_name, symbol):
        markets = getattr(self, "markets", {})
        market = markets.get(exchange_name, {}).get(symbol, {})
        limits = market.get("limits", {})
        amount_limits = limits.get("amount", {})
        cost_limits = limits.get("cost", {})
        return {
            "precision": market.get("precision", {}).get("amount"),
            "min_amount": amount_limits.get("min") or 0.0,
            "min_cost": cost_limits.get("min") or 0.0,
        }

    def normalize_amount(self, exchange_name, symbol, amount):
        client = self.clients[exchange_name]
        try:
            normalized = float(client.amount_to_precision(symbol, amount))
        except Exception:
            normalized = float(amount)
        constraints = self.market_constraints(exchange_name, symbol)
        if normalized < constraints["min_amount"]:
            raise RuntimeError(f"Order amount is below {symbol} minimum.")
        return normalized

    def fetch_ticker(self, exchange_name, symbol):
        client = self.clients[exchange_name]
        ticker = client.fetch_ticker(symbol)
        return {"bid": ticker["bid"], "ask": ticker["ask"]}

    def get_balance_usdt(self, exchange_name):
        client = self.clients[exchange_name]
        balance = client.fetch_balance()
        usdt = balance.get("USDT", {})
        return float(usdt.get("free", 0.0) or 0.0)

    def get_balance(self, exchange_name, currency):
        client = self.clients[exchange_name]
        balance = client.fetch_balance()
        account = balance.get(currency, {})
        return float(account.get("free", 0.0) or 0.0)

    def fetch_balances(self, exchange_name, currencies):
        """Return non-secret free/used/total balances for selected currencies."""
        balance = self.clients[exchange_name].fetch_balance()
        result = {}
        for currency in currencies:
            account = balance.get(currency, {})
            result[currency] = {
                "free": float(account.get("free", 0.0) or 0.0),
                "used": float(account.get("used", 0.0) or 0.0),
                "total": float(account.get("total", 0.0) or 0.0),
            }
        return result

    def value_balances_usdt(self, exchange_name, balances):
        """Value free and used balances in USDT using conservative market prices."""
        client = self.clients[exchange_name]
        total_free = 0.0
        total_used = 0.0
        for currency, values in balances.items():
            if currency == "USDT":
                price = 1.0
            else:
                direct = f"{currency}/USDT"
                inverse = f"USDT/{currency}"
                if direct in self.markets.get(exchange_name, {}):
                    ticker = client.fetch_ticker(direct)
                    price = float(ticker.get("bid") or 0.0)
                elif inverse in self.markets.get(exchange_name, {}):
                    ticker = client.fetch_ticker(inverse)
                    ask = float(ticker.get("ask") or 0.0)
                    price = 1.0 / ask if ask > 0 else 0.0
                else:
                    price = 0.0
            values["value_free_usdt"] = values["free"] * price
            values["value_used_usdt"] = values["used"] * price
            total_free += values["value_free_usdt"]
            total_used += values["value_used_usdt"]
        return {"free_usdt": total_free, "used_usdt": total_used,
                "total_usdt": total_free + total_used}

    def plan_buy_quantity(self, exchange_name, symbol, amount_usdt, price, fee=None):
        """Size a market buy from a price we already have, with fee headroom.

        Sizing off a price the caller just measured avoids a second ticker
        call, and avoids sizing against a price that differs from the book the
        slippage check just approved.
        """
        fee = self.taker_fee(exchange_name, symbol) if fee is None else fee
        minimum_cost = self.market_constraints(exchange_name, symbol)["min_cost"]
        if amount_usdt < minimum_cost:
            raise RuntimeError(f"Order cost is below {symbol} minimum.")
        if price <= 0:
            raise RuntimeError(f"No usable {symbol} price on {exchange_name}.")
        return self.normalize_amount(
            exchange_name, symbol, (amount_usdt / price) * (1.0 - fee))

    def place_market_buy(self, exchange_name, symbol, amount_usdt, fee=None):
        if not REAL_TRADING_ENABLED:
            raise RuntimeError("Real trading is disabled.")
        client = self.clients[exchange_name]
        ticker = client.fetch_ticker(symbol)
        ask = float(ticker["ask"])
        quantity = self.plan_buy_quantity(
            exchange_name, symbol, amount_usdt, ask, fee)
        return self._place_market_order(exchange_name, symbol, "buy", quantity)

    def _place_market_order(self, exchange_name, symbol, side, quantity):
        """Submit once with our client id and resolve an ambiguous timeout.

        A transport exception does not prove Binance rejected an order. It may
        have reached the matching engine before the reply was lost. Searching
        by the id generated here prevents a blind retry from doubling exposure.
        """
        client = self.clients[exchange_name]
        client_order_id = orders.new_client_order_id()
        params = {"clientOrderId": client_order_id}
        if ORDER_INTENT_HOOK:
            ORDER_INTENT_HOOK(client_order_id, exchange_name, symbol, side, quantity)
        limit_price = (getattr(self, "_approved_limit_prices", {}) or {}).get(
            (exchange_name, symbol, side))
        bounded = bool(getattr(self, "bounded_orders", False) and limit_price
                       and callable(getattr(client, "create_order", None)))
        if bounded:
            try:
                precise_price = float(client.price_to_precision(symbol, limit_price))
            except Exception:
                precise_price = float(limit_price)
            try:
                created = client.create_order(
                    symbol, "limit", side, float(quantity), precise_price,
                    {"timeInForce": "FOK", "clientOrderId": client_order_id})
            except Exception as exc:
                recovered = orders.find_by_client_id(client, symbol, client_order_id)
                if recovered is None:
                    if ORDER_RESULT_HOOK:
                        ORDER_RESULT_HOOK(client_order_id, "ambiguous", {"error": str(exc)})
                    raise orders.OrderReconciliationError(
                        f"Submitting protected {side} {symbol} on {exchange_name} "
                        f"failed ({exc}) and order {client_order_id} could not be "
                        "found. Verify the venue before resuming.",
                        symbol=symbol, client_order_id=client_order_id,
                        exchange=exchange_name) from exc
                created = recovered
            created = dict(created or {})
            created.setdefault("clientOrderId", client_order_id)
            if ORDER_RESULT_HOOK:
                ORDER_RESULT_HOOK(client_order_id, created.get("status") or "submitted", created)
            return created

        method = (client.create_market_buy_order if side == "buy"
                  else client.create_market_sell_order)
        # Tiny test doubles and a few old CCXT-compatible adapters expose only
        # (symbol, amount). Official CCXT clients accept the params argument.
        try:
            accepts_params = len(inspect.signature(method).parameters) >= 3
        except (TypeError, ValueError):
            accepts_params = True
        try:
            created = (method(symbol, quantity, params) if accepts_params
                       else method(symbol, quantity))
        except Exception as exc:
            recovered = orders.find_by_client_id(client, symbol, client_order_id)
            if recovered is None:
                if ORDER_RESULT_HOOK:
                    ORDER_RESULT_HOOK(client_order_id, "ambiguous", {"error": str(exc)})
                raise orders.OrderReconciliationError(
                    f"Submitting {side} {symbol} on {exchange_name} failed ({exc}) "
                    f"and order {client_order_id} could not be found. Treat the "
                    "position as open and verify it manually.",
                    symbol=symbol, client_order_id=client_order_id,
                    exchange=exchange_name) from exc
            created = recovered
        created = dict(created or {})
        created.setdefault("clientOrderId", client_order_id)
        if ORDER_RESULT_HOOK:
            ORDER_RESULT_HOOK(client_order_id, created.get("status") or "submitted", created)
        return created

    def place_market_buy_quantity(self, exchange_name, symbol, quantity):
        if not REAL_TRADING_ENABLED:
            raise RuntimeError("Real trading is disabled.")
        quantity = self.normalize_amount(exchange_name, symbol, quantity)
        return self._place_market_order(exchange_name, symbol, "buy", quantity)

    def place_market_sell(self, exchange_name, symbol, quantity):
        if not REAL_TRADING_ENABLED:
            raise RuntimeError("Real trading is disabled.")
        client = self.clients[exchange_name]
        quantity = self.normalize_amount(exchange_name, symbol, quantity)
        return self._place_market_order(exchange_name, symbol, "sell", quantity)

    def check_order_book(self, exchange_name, symbol, side, quantity, reference_price):
        """Reject thin books or prices that moved too far since scanning.

        Returns the price this specific size would average, not the best price.
        The old check compared `levels[0][0]` against the reference and summed
        the top ten sizes to prove depth: both pass on a book with dust on top
        and the real liquidity 2% away, which is the shape that turns a
        projected profit into a realized loss.
        """
        book = self.clients[exchange_name].fetch_order_book(
            symbol, limit=ORDER_BOOK_DEPTH)
        levels = book.get("asks" if side == "buy" else "bids", [])
        if not levels:
            raise RuntimeError(f"No {side} liquidity available on {exchange_name}.")

        estimate = books.fill_for_quantity(levels, quantity)
        if estimate.quantity <= ZERO:
            raise RuntimeError(f"No {side} liquidity available on {exchange_name}.")

        average = estimate.average_price
        reference = D(reference_price)
        if reference <= ZERO:
            raise RuntimeError(f"No reference price for {symbol} on {exchange_name}.")
        tolerance = D(MAX_SLIPPAGE_PCT) / HUNDRED
        drift = abs((average - reference) / reference * HUNDRED)
        if side == "buy" and average > reference * (ONE + tolerance):
            raise RuntimeError(
                f"Buy price moved beyond {MAX_SLIPPAGE_PCT:.2f}% on {exchange_name}: "
                f"{float(quantity):.8f} {symbol} would average "
                f"{float(average):.8f}, {float(drift):.3f}% above the quote.")
        if side == "sell" and average < reference * (ONE - tolerance):
            raise RuntimeError(
                f"Sell price moved beyond {MAX_SLIPPAGE_PCT:.2f}% on {exchange_name}: "
                f"{float(quantity):.8f} {symbol} would average "
                f"{float(average):.8f}, {float(drift):.3f}% below the quote.")

        if not estimate.complete:
            raise RuntimeError(
                f"Insufficient {side} liquidity on {exchange_name}: the visible "
                f"book covers {float(estimate.quantity):.8f} of "
                f"{float(quantity):.8f} {symbol}.")
        # Limit the actual order to the worst book level needed for this exact
        # quantity. FOK prevents an incomplete leg from resting or partially
        # committing while the rest of an arbitrage route moves away.
        consumed = max(1, int(estimate.levels_consumed))
        try:
            limit_price = float(levels[consumed - 1][0])
        except (IndexError, TypeError, ValueError):
            limit_price = float(average)
        approved = getattr(self, "_approved_limit_prices", None)
        if approved is None:
            approved = {}
            self._approved_limit_prices = approved
        approved[(exchange_name, symbol, side)] = limit_price
        return float(average)

    def _confirm_fill(self, exchange_name, symbol, side, requested_quantity, order):
        """Ask the exchange what actually filled, and take no other answer.

        The original engine read `filled` off the create-order response and
        fell back to `amount` - the size it had *asked* for - when the exchange
        left `filled` empty, which many do until the order is fetched again. It
        then sold coin it had never bought. `orders.reconcile_order` polls until
        the exchange states a size, or raises; it never guesses one.
        """
        client_order_id = str(order.get("clientOrderId") or order.get("client_order_id") or "")
        if ORDER_RESULT_HOOK and client_order_id:
            ORDER_RESULT_HOOK(client_order_id, "reconciling", order)
        fill = orders.reconcile_order(
            self.clients[exchange_name], exchange_name, symbol, side,
            requested_quantity, order,
            poll_timeout=getattr(self, "poll_timeout", orders.DEFAULT_POLL_TIMEOUT),
            poll_interval=getattr(self, "poll_interval", orders.DEFAULT_POLL_INTERVAL))
        if ORDER_RESULT_HOOK and fill.client_order_id:
            ORDER_RESULT_HOOK(fill.client_order_id, fill.status, fill.as_dict())
        return fill

    def _net_received(self, fill, currency, gross):
        """Gross fill minus a fee that was charged in the coin we received.

        Exchanges deduct the taker fee from the base coin on a buy unless the
        account pays fees in a discount token. Sizing the next leg off the
        gross figure asks to trade coin that is not there.
        """
        if (fill.fee_currency or "").upper() == (currency or "").upper():
            return max(0.0, float(gross) - float(fill.fee_cost))
        return float(gross)

    def _dust_tolerance(self, exchange_name, symbol, quantity):
        """Largest unsold remainder worth ignoring rather than halting over.

        Step-size flooring leaves a fraction of the base coin behind on almost
        every real fill. Treating that as an unhedged position would stop the
        bot after its first successful trade; treating a real shortfall as dust
        would hide a loss. The line is the exchange's own minimum order size,
        or 0.1% of the trade, whichever is larger - below it nothing can be
        traded anyway.
        """
        minimum = float(self.market_constraints(exchange_name, symbol)["min_amount"] or 0.0)
        return max(minimum, abs(float(quantity)) * 0.001)

    def execute_arbitrage(self, buy_exchange, sell_exchange, symbol,
                          amount_usdt, buy_price, sell_price):
        """Preflight balances, then submit both live market orders."""
        if not REAL_TRADING_ENABLED or EXECUTION_MODE != "real":
            raise RuntimeError("Real execution is not explicitly enabled.")

        buy_fee = self.taker_fee(buy_exchange, symbol)
        sell_fee = self.taker_fee(sell_exchange, symbol)
        base = base_coin(symbol)
        quote = quote_coin(symbol)
        estimated_quantity = (amount_usdt / buy_price) * (1.0 - buy_fee)
        if self.get_balance_usdt(buy_exchange) < amount_usdt:
            raise RuntimeError(f"Insufficient USDT balance on {buy_exchange}.")
        if self.get_balance(sell_exchange, base) < estimated_quantity:
            raise RuntimeError(f"Insufficient {base} balance on {sell_exchange}.")
        live_buy_price = self.check_order_book(
            buy_exchange, symbol, "buy", estimated_quantity, buy_price)
        live_sell_price = self.check_order_book(
            sell_exchange, symbol, "sell", estimated_quantity, sell_price)
        # Fees compound rather than add: the sell fee lands on the grossed-up
        # proceeds, so subtracting 2*fee overstates the edge every time.
        conservative_profit_pct = float(net_spread_pct(
            live_buy_price, live_sell_price, buy_fee, sell_fee))
        if conservative_profit_pct < MIN_PROFIT_PCT:
            raise RuntimeError(
                f"Live profit dropped to {conservative_profit_pct:.3f}%, "
                f"below the {MIN_PROFIT_PCT:.3f}% minimum."
            )

        requested_buy = self.plan_buy_quantity(
            buy_exchange, symbol, amount_usdt, live_buy_price, buy_fee)
        buy_order = self.place_market_buy_quantity(
            buy_exchange, symbol, requested_buy)
        try:
            buy_fill = self._confirm_fill(
                buy_exchange, symbol, "buy", requested_buy, buy_order)
        except orders.OrderReconciliationError as exc:
            # The order exists and its size is unknown. That is a position.
            raise UnhedgedPositionError(
                buy_exchange, sell_exchange, symbol, requested_buy, buy_order,
                exc, recovery_exchange=buy_exchange,
                quantity_confirmed=False) from exc
        filled_quantity = float(buy_fill.filled_quantity)
        if filled_quantity <= 0:
            raise RuntimeError("Buy order returned no filled quantity.")

        try:
            self.check_order_book(
                sell_exchange, symbol, "sell", filled_quantity, sell_price)
            sell_order = self.place_market_sell(sell_exchange, symbol, filled_quantity)
        except Exception as exc:
            raise UnhedgedPositionError(
                buy_exchange, sell_exchange, symbol, filled_quantity,
                buy_order, exc, recovery_exchange=buy_exchange,
            ) from exc
        try:
            sell_fill = self._confirm_fill(
                sell_exchange, symbol, "sell", filled_quantity, sell_order)
        except orders.OrderReconciliationError as exc:
            raise UnhedgedPositionError(
                buy_exchange, sell_exchange, symbol, filled_quantity,
                buy_order, exc, recovery_exchange=sell_exchange,
                quantity_confirmed=False) from exc
        filled_sell = float(sell_fill.filled_quantity)
        shortfall = filled_quantity - filled_sell
        if shortfall > self._dust_tolerance(sell_exchange, symbol, filled_quantity):
            raise UnhedgedPositionError(
                buy_exchange, sell_exchange, symbol, max(0.0, shortfall),
                buy_order, RuntimeError("Sell leg was partially filled."),
                recovery_exchange=sell_exchange,
            )

        buy_cost = float(buy_fill.cost) or float(buy_order.get("cost") or amount_usdt)
        sell_proceeds = float(sell_fill.cost) or float(
            sell_order.get("cost") or (filled_sell * live_sell_price))
        # Quote-currency fees are a cash cost the `cost` fields do not include.
        # A fee charged in the base coin already shows up as a smaller sell.
        quote_fees = sum(
            float(fill.fee_cost) for fill in (buy_fill, sell_fill)
            if (fill.fee_currency or "").upper() == quote.upper())
        return {
            "buy_order": buy_order,
            "sell_order": sell_order,
            "buy_fill": buy_fill.as_dict(),
            "sell_fill": sell_fill.as_dict(),
            "filled_quantity": filled_quantity,
            "unsold_dust": max(0.0, shortfall),
            "profit_usdt": sell_proceeds - buy_cost - quote_fees,
        }

    def execute_triangular(self, exchange_name, start_usdt, prices, symbols=None):
        """Execute USDT -> BTC -> ETH -> USDT with recovery on any failed leg."""
        if not REAL_TRADING_ENABLED or EXECUTION_MODE != "real":
            raise RuntimeError("Real execution is not explicitly enabled.")
        if self.get_balance_usdt(exchange_name) < start_usdt:
            raise RuntimeError(f"Insufficient USDT balance on {exchange_name}.")

        symbols = symbols or ["BTC/USDT", "ETH/BTC", "ETH/USDT"]
        first_symbol, middle_symbol, final_symbol = symbols
        first_price = prices[first_symbol]
        middle_price = prices[middle_symbol]
        final_price = prices[final_symbol]
        fee = self.taker_fee(exchange_name, symbols[0])
        btc_quantity = (start_usdt / first_price) * (1 - fee)
        eth_quantity = (btc_quantity / middle_price) * (1 - fee)
        live_btc_price = self.check_order_book(
            exchange_name, first_symbol, "buy", btc_quantity, first_price)
        live_eth_btc_price = self.check_order_book(
            exchange_name, middle_symbol, "buy", eth_quantity, middle_price)
        live_eth_usdt_price = self.check_order_book(
            exchange_name, final_symbol, "sell", eth_quantity, final_price)

        conservative_final = (
            start_usdt * (1 - fee) / live_btc_price
            * (1 - fee) / live_eth_btc_price
            * live_eth_usdt_price * (1 - fee)
        )
        conservative_profit_pct = (conservative_final - start_usdt) / start_usdt * 100
        if conservative_profit_pct < MIN_PROFIT_PCT:
            raise RuntimeError(
                f"Live profit dropped to {conservative_profit_pct:.3f}%, "
                f"below the {MIN_PROFIT_PCT:.3f}% minimum."
            )

        # From here the capital is committed. A leg that cannot fill leaves a
        # real position in an intermediate coin, so every failure below is an
        # UnhedgedPositionError - the only error the server records for
        # recovery - and never a bare RuntimeError.
        requested_btc = self.plan_buy_quantity(
            exchange_name, first_symbol, start_usdt, live_btc_price, fee)
        btc_order = self.place_market_buy_quantity(
            exchange_name, first_symbol, requested_btc)
        try:
            btc_fill = self._confirm_fill(
                exchange_name, first_symbol, "buy", requested_btc, btc_order)
        except orders.OrderReconciliationError as exc:
            raise UnhedgedPositionError(
                exchange_name, exchange_name, first_symbol, requested_btc,
                btc_order, exc, quantity_confirmed=False) from exc
        filled_btc = float(btc_fill.filled_quantity)
        if filled_btc <= 0:
            raise RuntimeError(f"{first_symbol} buy order returned no filled quantity.")
        # A partial first leg is not a reason to strand the position: the route
        # is simply run at the size we really hold. Deducting a fee charged in
        # the base coin matters, or the next leg asks for coin we do not have.
        held_btc = self._net_received(btc_fill, base_coin(first_symbol), filled_btc)

        try:
            requested_eth = self.plan_buy_quantity(
                exchange_name, middle_symbol, held_btc, live_eth_btc_price, fee)
            self.check_order_book(
                exchange_name, middle_symbol, "buy", requested_eth,
                live_eth_btc_price)
            eth_order = self.place_market_buy_quantity(
                exchange_name, middle_symbol, requested_eth)
        except Exception as exc:
            raise UnhedgedPositionError(
                exchange_name, exchange_name, first_symbol, held_btc, btc_order, exc
            ) from exc
        try:
            eth_fill = self._confirm_fill(
                exchange_name, middle_symbol, "buy", requested_eth, eth_order)
        except orders.OrderReconciliationError as exc:
            raise UnhedgedPositionError(
                exchange_name, exchange_name, middle_symbol, requested_eth,
                eth_order, exc, quantity_confirmed=False) from exc
        filled_eth = float(eth_fill.filled_quantity)
        if filled_eth <= 0:
            raise UnhedgedPositionError(
                exchange_name, exchange_name, first_symbol, held_btc, btc_order,
                RuntimeError(f"{middle_symbol} buy order returned no filled quantity."),
            )
        # Whatever BTC the middle leg did not spend is still sitting there. It
        # is reported rather than silently forgotten, but it is dust-sized and
        # does not stop the loop from closing.
        residual_btc = max(0.0, held_btc - float(eth_fill.cost))
        held_eth = self._net_received(eth_fill, base_coin(middle_symbol), filled_eth)

        try:
            self.check_order_book(
                exchange_name, final_symbol, "sell", held_eth,
                live_eth_usdt_price)
            eth_sell_order = self.place_market_sell(
                exchange_name, final_symbol, held_eth)
        except Exception as exc:
            raise UnhedgedPositionError(
                exchange_name, exchange_name, final_symbol, held_eth, eth_order, exc
            ) from exc
        try:
            sell_fill = self._confirm_fill(
                exchange_name, final_symbol, "sell", held_eth, eth_sell_order)
        except orders.OrderReconciliationError as exc:
            raise UnhedgedPositionError(
                exchange_name, exchange_name, final_symbol, held_eth,
                eth_sell_order, exc, quantity_confirmed=False) from exc
        filled_sell = float(sell_fill.filled_quantity)
        shortfall = held_eth - filled_sell
        if shortfall > self._dust_tolerance(exchange_name, final_symbol, held_eth):
            raise UnhedgedPositionError(
                exchange_name, exchange_name, final_symbol, max(0.0, shortfall),
                eth_order, RuntimeError("Final triangular leg was partially filled."),
            )

        quote = quote_coin(final_symbol)
        start_cost = float(btc_fill.cost) or float(btc_order.get("cost") or start_usdt)
        proceeds = float(sell_fill.cost) or float(
            eth_sell_order.get("cost") or (filled_sell * live_eth_usdt_price))
        quote_fees = sum(
            float(leg.fee_cost) for leg in (btc_fill, sell_fill)
            if (leg.fee_currency or "").upper() == quote.upper())
        return {
            "buy_order": btc_order,
            "middle_order": eth_order,
            "sell_order": eth_sell_order,
            "legs": [btc_fill.as_dict(), eth_fill.as_dict(), sell_fill.as_dict()],
            "residual_base": residual_btc,
            "unsold_dust": max(0.0, shortfall),
            "profit_usdt": proceeds - start_cost - quote_fees,
        }


# ==================================================================
#  PAPER WALLET  —  virtual balances, one per exchange per symbol
# ==================================================================

def base_coin(symbol):
    """'BTC/USDT' -> 'BTC'"""
    return symbol.split("/")[0]


def quote_coin(symbol):
    """'BTC/USDT' -> 'USDT' - the currency fees and profit are measured in."""
    parts = symbol.split("/")
    return parts[1] if len(parts) > 1 else ""


class PaperWallet:
    """Holds virtual USDT and coins on every exchange."""

    def __init__(self, exchanges, symbols, cash, start_prices):
        self.usdt = {}   # usdt[exchange][symbol]
        self.coin = {}   # coin[exchange][symbol]
        for ex in exchanges:
            self.usdt[ex] = {}
            self.coin[ex] = {}
            for sym in symbols:
                # Start with HALF cash, HALF coin on every exchange.
                # Real arbitrage works this way: you must already hold
                # the coin on the expensive exchange to sell instantly.
                self.usdt[ex][sym] = cash / 2
                self.coin[ex][sym] = (cash / 2) / start_prices[sym]

    def buy(self, exchange, symbol, spend_usdt, price, fee):
        """Buy coin on `exchange`, spending `spend_usdt`. Returns coin amount."""
        if self.usdt[exchange][symbol] < spend_usdt:
            return None
        received = (spend_usdt * (1 - fee)) / price
        self.usdt[exchange][symbol] -= spend_usdt
        self.coin[exchange][symbol] += received
        return received

    def sell(self, exchange, symbol, coin_amount, price, fee):
        """Sell coin on `exchange`. Returns USDT received, or None if not enough."""
        if self.coin[exchange][symbol] < coin_amount:
            return None
        received = coin_amount * price * (1 - fee)
        self.coin[exchange][symbol] -= coin_amount
        self.usdt[exchange][symbol] += received
        return received

    def execute_arbitrage(self, buy_exchange, sell_exchange, symbol,
                          spend_usdt, buy_price, sell_price, fee):
        """Execute both paper legs only when both balances can support them."""
        coin_amount = (spend_usdt * (1 - fee)) / buy_price
        if self.usdt[buy_exchange][symbol] < spend_usdt:
            return None
        if self.coin[sell_exchange][symbol] < coin_amount:
            return None

        self.usdt[buy_exchange][symbol] -= spend_usdt
        self.coin[buy_exchange][symbol] += coin_amount
        self.coin[sell_exchange][symbol] -= coin_amount
        received = coin_amount * sell_price * (1 - fee)
        self.usdt[sell_exchange][symbol] += received
        return coin_amount, received

    def total_value(self, mid_prices):
        """Total portfolio value in USDT at current prices."""
        total = 0.0
        for ex in self.usdt:
            for sym in self.usdt[ex]:
                total += self.usdt[ex][sym]
                total += self.coin[ex][sym] * mid_prices[sym]
        return total


# ==================================================================
#  PRICE FEEDS
# ==================================================================

# Rough starting prices for demo mode (they random-walk from here)
DEMO_START_PRICES = {
    "BTC/USDT": 100000.0,
    "ETH/USDT": 3500.0,
    "SOL/USDT": 200.0,
    "XRP/USDT": 0.60,
    "DOGE/USDT": 0.15,
}


class DemoFeed:
    """
    Generates FAKE prices so you can test without internet.
    Each exchange gets its own slightly different price for the
    same coin - that's what creates arbitrage gaps.
    """

    def __init__(self, exchanges, symbols):
        self.exchanges = exchanges
        self.symbols = symbols
        self.mid = {s: DEMO_START_PRICES[s] for s in symbols}
        # each exchange drifts a little above/below the mid price
        self.offset = {ex: {s: 0.0 for s in symbols} for ex in exchanges}

    def get_quotes(self):
        """Returns quotes[symbol][exchange] = {'bid': x, 'ask': y}"""
        quotes = {}
        for sym in self.symbols:
            # random walk: mid price moves up/down a little
            self.mid[sym] *= 1 + random.uniform(-0.001, 0.001)
            quotes[sym] = {}
            for ex in self.exchanges:
                # occasionally open a gap on one exchange
                if random.random() < DEMO_GAP_CHANCE:
                    self.offset[ex][sym] = random.uniform(-0.006, 0.006)
                else:
                    # gap slowly closes again (like real markets)
                    self.offset[ex][sym] *= 0.7
                price = self.mid[sym] * (1 + self.offset[ex][sym])
                # spread between bid and ask (~0.02%)
                quotes[sym][ex] = {
                    "bid": price * 0.9999,
                    "ask": price * 1.0001,
                }
        return quotes


class LiveFeed:
    """
    Fetches REAL prices from the exchanges using the ccxt library.
    If REAL_TRADING_ENABLED is on, it expects valid API keys for each exchange.
    """

    def __init__(self, exchanges, symbols=None):
        import ccxt  # imported here so demo mode doesn't need it
        config_result = validate_real_trading_config()
        if REAL_TRADING_ENABLED and not config_result["ok"]:
            raise SystemExit(config_result["message"])

        self.clients = {}
        self.markets = {}
        for name in exchanges:
            try:
                if REAL_TRADING_ENABLED:
                    self.clients[name] = create_exchange_client(name)
                else:
                    self.clients[name] = getattr(ccxt, name)({"enableRateLimit": True})
                self.markets[name] = self.clients[name].load_markets()
            except Exception as e:
                print(f"  [!] Could not connect to {name}: {e}")
        required_clients = 2 if TRADING_STRATEGY == "cross_exchange" else 1
        if len(self.clients) < required_clients:
            raise SystemExit(
                f"Need at least {required_clients} working exchange(s) for {TRADING_STRATEGY}."
            )
        self.routes = []
        self.route_candidates = 0
        if TRADING_STRATEGY == "triangular":
            first_client = next(iter(self.clients))
            discovered = discover_triangular_routes(self.markets[first_client])
            # When an operator supplied a symbol whitelist, never discover a
            # route outside it. This is essential for exchange-side symbol
            # whitelists: finding a profitable fourth pair that the API key is
            # forbidden to trade would strand the preceding leg.
            allowed_symbols = set(symbols or [])
            if allowed_symbols:
                discovered = [route for route in discovered
                              if set(route.get("symbols") or []).issubset(allowed_symbols)]
            # Every discovered route is arithmetically valid; almost none are
            # tradeable. Ranking by the thinnest leg's volume and keeping the
            # top slice is what turns a multi-minute scan into a fast one, and
            # it drops exactly the routes whose tickers lie about their depth.
            self.route_candidates = len(discovered)
            self.routes = feed.rank_routes(
                discovered, self._volume_snapshot(first_client),
                limit=MAX_TRIANGULAR_ROUTES)
        route_symbols = feed.symbols_for_routes(self.routes)
        self.symbols = list(dict.fromkeys(route_symbols or (symbols or SYMBOLS)))
        self.rejected_quotes = {}
        self.last_fetch_seconds = 0.0
        self.last_source = "rest"
        self.stream = None
        self.stream_error = ""
        if os.environ.get("ARBICORE_STREAMING", "0") == "1":
            try:
                self.stream = streaming.CcxtProStream(
                    list(self.clients), self.symbols, sandbox=SANDBOX_MODE)
                self.stream.start()
            except Exception as exc:
                # Streaming is an optimization, never a reason to lose the
                # well-tested REST path. Health exposes this degradation.
                self.stream = None
                self.stream_error = f"{type(exc).__name__}: {exc}"

    def _volume_snapshot(self, exchange_name):
        """24h volumes for route ranking, empty if the venue will not say.

        Ranking is a startup nicety, not a safety check, so a venue that
        cannot answer simply leaves the discovery order in place.
        """
        client = self.clients[exchange_name]
        if not feed.supports_bulk_tickers(client):
            return {}
        try:
            return client.fetch_tickers() or {}
        except Exception:
            return {}

    def get_quotes(self):
        """One bulk request per venue, in parallel, stale prices discarded.

        The venues are independent, so waiting for each in turn adds their
        latencies together for no reason. Threads are the right tool: this is
        pure network wait, and ccxt clients are not shared across them.
        """
        started = time.perf_counter()
        active_stream = getattr(self, "stream", None)
        if active_stream and active_stream.cache.complete(list(self.clients), self.symbols):
            collected = active_stream.cache.snapshot(list(self.clients), self.symbols)
            self.rejected_quotes = {}
            self.last_fetch_seconds = time.perf_counter() - started
            self.last_source = "websocket"
            return collected
        now_ms = time.time() * 1000.0
        collected = {}
        rejected = {}

        def pull(item):
            name, client = item
            fetcher = (feed.fetch_bulk_quotes if feed.supports_bulk_tickers(client)
                       else feed.fetch_quotes_one_by_one)
            try:
                return name, fetcher(client, self.symbols, now_ms=now_ms,
                                     max_age_ms=MAX_QUOTE_AGE_MS)
            except Exception as exc:
                return name, ({}, {"*": f"feed error ({exc})"})

        clients = list(self.clients.items())
        if len(clients) == 1:
            results = [pull(clients[0])]
        else:
            with futures.ThreadPoolExecutor(max_workers=len(clients)) as pool:
                results = list(pool.map(pull, clients))

        for name, (venue_quotes, venue_rejected) in results:
            for symbol, quote in venue_quotes.items():
                normalized = {"bid": quote["bid"], "ask": quote["ask"]}
                if quote.get("age_ms"):
                    normalized["age_ms"] = quote["age_ms"]
                collected.setdefault(symbol, {})[name] = normalized
            if venue_rejected:
                rejected[name] = venue_rejected

        self.rejected_quotes = rejected
        self.last_fetch_seconds = time.perf_counter() - started
        self.last_source = "rest_fallback" if active_stream else "rest"
        return collected


# ==================================================================
#  THE ARBITRAGE SCANNER
# ==================================================================

def find_opportunity(quotes_for_symbol):
    """
    Given quotes[exchange] = {'bid','ask'} for one symbol,
    find the best buy (lowest ask) and best sell (highest bid).
    Returns (buy_exchange, sell_exchange, buy_price, sell_price,
             net_profit_pct) or None if no profitable gap.
    """
    best_ask_ex = min(quotes_for_symbol, key=lambda e: quotes_for_symbol[e]["ask"])
    best_bid_ex = max(quotes_for_symbol, key=lambda e: quotes_for_symbol[e]["bid"])

    if best_ask_ex == best_bid_ex:
        return None  # same exchange -> not cross-exchange arbitrage

    ask = quotes_for_symbol[best_ask_ex]["ask"]
    bid = quotes_for_symbol[best_bid_ex]["bid"]
    if bid <= ask:
        return None  # no gap

    # Profit math: buy 1 coin costs `ask` (+fee), selling gives `bid` (-fee).
    # The fees compound - the sell fee applies to the grossed-up proceeds - so
    # subtracting 2*fee from the gross spread overstates the edge, always in
    # the direction that loses money.
    gross_pct = (bid - ask) / ask * 100          # before fees, for reporting
    net_pct = float(net_spread_pct(ask, bid, TAKER_FEE, TAKER_FEE))
    if gross_pct <= 0:
        return None

    if net_pct >= MIN_PROFIT_PCT:
        return best_ask_ex, best_bid_ex, ask, bid, net_pct
    return None


def find_triangular_opportunity(quotes, exchange, start_usdt, fee):
    """Calculate USDT -> BTC -> ETH -> USDT on one exchange."""
    cycle = calculate_triangular_route(
        quotes, exchange, start_usdt, fee,
        ["BTC/USDT", "ETH/BTC", "ETH/USDT"],
        ["USDT", "BTC", "ETH", "USDT"],
    )
    if cycle is None or cycle["profit_pct"] < MIN_PROFIT_PCT:
        return None
    return cycle


def calculate_triangular_cycle(quotes, exchange, start_usdt, fee):
    """Return the current three-leg cycle result, profitable or not."""
    return calculate_triangular_route(
        quotes, exchange, start_usdt, fee,
        ["BTC/USDT", "ETH/BTC", "ETH/USDT"],
        ["USDT", "BTC", "ETH", "USDT"],
    )


def calculate_triangular_route(quotes, exchange, start_usdt, fee, symbols, route):
    """Evaluate a three-market route using ask, ask, then bid prices."""
    if len(symbols) != 3 or len(route) != 4:
        raise ValueError("A triangular route requires three symbols and four assets.")
    if any(symbol not in quotes or exchange not in quotes[symbol] for symbol in symbols):
        return None

    first_quote = quotes[symbols[0]][exchange]
    second_quote = quotes[symbols[1]][exchange]
    final_quote = quotes[symbols[2]][exchange]
    first_amount = (start_usdt * (1 - fee)) / first_quote["ask"]
    second_amount = (first_amount * (1 - fee)) / second_quote["ask"]
    final_usdt = second_amount * final_quote["bid"] * (1 - fee)
    profit = final_usdt - start_usdt
    profit_pct = (profit / start_usdt) * 100
    return {
        "exchange": exchange,
        "route": route,
        "profit_usdt": profit,
        "profit_pct": profit_pct,
        "symbols": symbols,
        "prices": {symbols[0]: first_quote["ask"],
                   symbols[1]: second_quote["ask"],
                   symbols[2]: final_quote["bid"]},
        "final_usdt": final_usdt,
    }


def discover_triangular_routes(markets, quote_currency="USDT"):
    """Find quote/base cycles such as USDT -> BTC -> ETH -> USDT."""
    symbols = set(markets)
    assets = set()
    for symbol in symbols:
        if "/" not in symbol:
            continue
        base, quote = symbol.split("/", 1)
        assets.update((base, quote))

    routes = []
    for first_asset in assets:
        if first_asset == quote_currency:
            continue
        first = f"{first_asset}/{quote_currency}"
        if first not in symbols:
            continue
        for second_asset in assets:
            if second_asset in (quote_currency, first_asset):
                continue
            second = f"{second_asset}/{first_asset}"
            final = f"{second_asset}/{quote_currency}"
            if second in symbols and final in symbols:
                routes.append({
                    "route": [quote_currency, first_asset, second_asset, quote_currency],
                    "symbols": [first, second, final],
                })
    return routes


def find_best_triangular_opportunity(quotes, exchange, start_usdt, fee, routes):
    """Return the highest-profit supported route above the configured floor."""
    candidates = []
    for route in routes:
        result = calculate_triangular_route(
            quotes, exchange, start_usdt, fee,
            route["symbols"], route["route"],
        )
        if result and result["profit_pct"] >= MIN_PROFIT_PCT:
            candidates.append(result)
    return max(candidates, key=lambda item: item["profit_pct"], default=None)


# ==================================================================
#  LOGGING
# ==================================================================

CSV_HEADER = [
    "time", "symbol", "buy_exchange", "sell_exchange",
    "buy_price", "sell_price", "trade_size_usdt",
    "profit_usdt", "net_profit_pct", "buy_order_id",
    "sell_order_id", "status", "middle_order_id",
]


def init_csv():
    """Make sure the log exists and has a header, without destroying history.

    This used to open the file in "w" mode, so every restart of the CLI or the
    web server silently erased the entire trade log - including the record of
    real orders, which is the one file an operator cannot reconstruct.
    """
    path = Path(LOG_FILE)
    if path.exists() and path.stat().st_size > 0:
        return
    with open(LOG_FILE, "a", newline="") as f:
        csv.writer(f).writerow(CSV_HEADER)


def log_trade(row):
    with open(LOG_FILE, "a", newline="") as f:
        csv.writer(f).writerow(row)


# ==================================================================
#  MAIN LOOP
# ==================================================================

def main():
    load_live_config_if_present()
    validation = validate_real_trading_config() if MODE == "live" else {"ok": True}
    if MODE == "live" and not validation["ok"]:
        raise SystemExit(validation["message"])

    print("=" * 64)
    print("  CROSS-EXCHANGE ARBITRAGE BOT  —  PAPER TRADING (no real money)")
    if REAL_TRADING_ENABLED:
        print("  REAL TRADING ENABLED: live orders are allowed only after explicit opt-in.")
    print(f"  Mode: {MODE.upper()}   |   Scanning {len(SYMBOLS)} pairs "
          f"every {CHECK_INTERVAL}s")
    print("  Press Ctrl+C to stop.")
    print("=" * 64)

    if MODE == "live":
        feed = LiveFeed(EXCHANGES)
        start_prices = {}
        print("  Fetching starting prices...", end=" ", flush=True)
        first = feed.get_quotes()
        for sym in SYMBOLS:
            if sym in first and first[sym]:
                prices = [q["bid"] for q in first[sym].values()]
                start_prices[sym] = sum(prices) / len(prices)
        print("done.")
    else:
        feed = DemoFeed(EXCHANGES, SYMBOLS)
        start_prices = dict(DEMO_START_PRICES)

    active_symbols = [s for s in SYMBOLS if s in start_prices]
    wallet = PaperWallet(EXCHANGES, active_symbols,
                         START_CASH_PER_EXCHANGE, start_prices)
    start_value = wallet.total_value(start_prices)

    init_csv()
    scan_count = 0
    trades_count = 0
    total_profit = 0.0

    try:
        while True:
            scan_count += 1
            quotes = feed.get_quotes()
            now = datetime.now().strftime("%H:%M:%S")

            # mid price per symbol (average across exchanges) for valuation
            mid_prices = {}
            for sym in active_symbols:
                if sym in quotes and quotes[sym]:
                    mids = [(q["bid"] + q["ask"]) / 2
                            for q in quotes[sym].values()]
                    mid_prices[sym] = sum(mids) / len(mids)

            found_this_scan = []
            for sym in active_symbols:
                if sym not in quotes or len(quotes[sym]) < 2:
                    continue
                opp = find_opportunity(quotes[sym])
                if not opp:
                    continue
                buy_ex, sell_ex, ask, bid, net_pct = opp

                # ---- execute the PAPER trade ----
                result = wallet.execute_arbitrage(
                    buy_ex, sell_ex, sym, TRADE_SIZE_USDT, ask, bid, TAKER_FEE)
                if result is None:
                    continue
                _, usdt_received = result

                profit = usdt_received - TRADE_SIZE_USDT
                total_profit += profit
                trades_count += 1
                found_this_scan.append(
                    (sym, buy_ex, sell_ex, ask, bid, profit, net_pct))
                log_trade([datetime.now().isoformat(timespec="seconds"),
                           sym, buy_ex, sell_ex, f"{ask:.6f}", f"{bid:.6f}",
                           TRADE_SIZE_USDT, f"{profit:.4f}", f"{net_pct:.3f}",
                           "", "", "paper", ""])

            # ---- console output ----
            if found_this_scan:
                for sym, bex, sex, ask, bid, profit, net_pct in found_this_scan:
                    print(f"[{now}] OPPORTUNITY {sym:<9} "
                          f"buy {bex:<8} @ {ask:,.4f}  ->  "
                          f"sell {sex:<8} @ {bid:,.4f}  |  "
                          f"net {net_pct:+.2f}%  =  ${profit:+.2f}")
            else:
                print(f"[{now}] scan #{scan_count}: no profitable gap "
                      f"(fees eat small gaps)", end="\r")

            # summary every 10 scans
            if scan_count % 10 == 0 and mid_prices:
                value = wallet.total_value(mid_prices)
                print(f"\n--- SUMMARY after {scan_count} scans "
                      f"| trades: {trades_count} "
                      f"| realized arb profit: ${total_profit:+.2f} "
                      f"| portfolio: ${value:,.2f} "
                      f"(started ${start_value:,.2f}) ---\n")

            time.sleep(CHECK_INTERVAL)

    except KeyboardInterrupt:
        print("\n\nStopped by user.")
        if trades_count:
            print(f"Final: {trades_count} paper trades, "
                  f"realized arbitrage profit: ${total_profit:+.2f}")
            print(f"Trade log saved to: {LOG_FILE}")
        else:
            print("No trades were triggered. Try lowering MIN_PROFIT_PCT "
                  "or running longer.")


if __name__ == "__main__":
    main()
