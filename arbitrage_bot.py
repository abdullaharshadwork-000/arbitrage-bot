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
import random
import time
from datetime import datetime
from pathlib import Path

# ==================================================================
#  CONFIGURATION  —  everything you might want to change is here
# ==================================================================

MODE = "demo"            # "demo" = fake prices | "live" = real prices
EXECUTION_MODE = "paper" # "paper" = simulated orders | "real" = live orders
TRADING_STRATEGY = "cross_exchange"  # or "triangular"

# IMPORTANT: real trading is disabled by default to stop accidental orders.
REAL_TRADING_ENABLED = False
EXCHANGE_CREDENTIALS = {
    "binance": {"apiKey": "", "secret": ""},
    "kucoin": {"apiKey": "", "secret": ""},
    "okx": {"apiKey": "", "secret": ""},
    "bybit": {"apiKey": "", "secret": ""},
}

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

CHECK_INTERVAL = 5        # seconds between scans

LOG_FILE = "trades.csv"   # every simulated trade is saved here

# Demo mode only: how jumpy the fake market is
DEMO_GAP_CHANCE = 0.25    # chance per scan that a price gap opens
                          # (real markets gap far less often!)


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
        creds = EXCHANGE_CREDENTIALS.get(exchange, {})
        key = creds.get("apiKey")
        secret = creds.get("secret")
        if key and secret:
            configured.append(exchange)
        elif key or secret:
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

        for key in ("REAL_TRADING_ENABLED", "MODE", "EXECUTION_MODE", "TRADING_STRATEGY", "EXCHANGES", "EXCHANGE_CREDENTIALS"):
            if hasattr(module, key):
                globals()[key] = getattr(module, key)

        for key in ("TRADE_SIZE_USDT", "TAKER_FEE", "MIN_PROFIT_PCT", "CHECK_INTERVAL", "MAX_SLIPPAGE_PCT"):
            if hasattr(module, key):
                globals()[key] = getattr(module, key)

        EXCHANGES_MASTER = list(EXCHANGES)
        SYMBOLS_MASTER = list(SYMBOLS)

        # Keep demo defaults if the live config leaves placeholders empty.
        if isinstance(EXCHANGE_CREDENTIALS, dict):
            for exchange, creds in list(EXCHANGE_CREDENTIALS.items()):
                if isinstance(creds, dict) and "apiKey" in creds and creds["apiKey"] == "PASTE_":
                    EXCHANGE_CREDENTIALS[exchange] = {"apiKey": "", "secret": ""}

        return True
    except Exception as exc:
        print(f"[!] Could not load live config: {exc}")
        return False


def create_exchange_client(exchange_name):
    """Build a CCXT client using configured API credentials."""
    import ccxt

    creds = EXCHANGE_CREDENTIALS.get(exchange_name, {})
    config = {"enableRateLimit": True}
    api_key = creds.get("apiKey")
    secret = creds.get("secret")
    if api_key:
        config["apiKey"] = api_key
    if secret:
        config["secret"] = secret

    exchange_class = getattr(ccxt, exchange_name, None)
    if exchange_class is None:
        raise ValueError(f"Unsupported exchange: {exchange_name}")
    return exchange_class(config)


class UnhedgedPositionError(RuntimeError):
    """Raised when a filled buy cannot be paired with its sell order."""

    def __init__(self, buy_exchange, sell_exchange, symbol, quantity,
                 buy_order, cause, recovery_exchange=None):
        self.buy_exchange = buy_exchange
        self.sell_exchange = sell_exchange
        self.symbol = symbol
        self.quantity = quantity
        self.buy_order = buy_order
        self.cause = cause
        self.recovery_exchange = recovery_exchange or buy_exchange
        order_id = buy_order.get("id") or "unknown"
        super().__init__(
            f"Sell failed after buy order {order_id} filled: {cause}. Manual recovery required."
        )


class RealExecutionEngine:
    """Thin wrapper around live exchange APIs with strict safety checks."""

    def __init__(self, exchanges):
        self.exchanges = exchanges
        self.clients = {name: create_exchange_client(name) for name in exchanges}
        self.markets = {}
        self.fee_rates = {}
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
        """Check public markets and private balance access without placing orders."""
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
            "error": None,
        }
        try:
            client.fetch_ticker("BTC/USDT")
            result["balance_access"] = bool(client.fetch_balance())
            if hasattr(client, "fetch_trading_fee"):
                fee = client.fetch_trading_fee("BTC/USDT")
                taker = fee.get("taker") if isinstance(fee, dict) else None
                result["fee"] = float(taker) if taker is not None else None
            result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
            result["ok"] = result["balance_access"] and market_count > 0
        except Exception as exc:
            result["error"] = str(exc)
            result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
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

    def place_market_buy(self, exchange_name, symbol, amount_usdt, fee=None):
        if not REAL_TRADING_ENABLED:
            raise RuntimeError("Real trading is disabled.")
        client = self.clients[exchange_name]
        ticker = client.fetch_ticker(symbol)
        ask = float(ticker["ask"])
        fee = self.taker_fee(exchange_name, symbol) if fee is None else fee
        quantity = self.normalize_amount(
            exchange_name, symbol, (amount_usdt / ask) * (1.0 - fee))
        minimum_cost = self.market_constraints(exchange_name, symbol)["min_cost"]
        if amount_usdt < minimum_cost:
            raise RuntimeError(f"Order cost is below {symbol} minimum.")
        return client.create_market_buy_order(symbol, quantity)

    def place_market_buy_quantity(self, exchange_name, symbol, quantity):
        if not REAL_TRADING_ENABLED:
            raise RuntimeError("Real trading is disabled.")
        quantity = self.normalize_amount(exchange_name, symbol, quantity)
        return self.clients[exchange_name].create_market_buy_order(symbol, quantity)

    def place_market_sell(self, exchange_name, symbol, quantity):
        if not REAL_TRADING_ENABLED:
            raise RuntimeError("Real trading is disabled.")
        client = self.clients[exchange_name]
        quantity = self.normalize_amount(exchange_name, symbol, quantity)
        return client.create_market_sell_order(symbol, quantity)

    def check_order_book(self, exchange_name, symbol, side, quantity, reference_price):
        """Reject thin books or prices that moved too far since scanning."""
        book = self.clients[exchange_name].fetch_order_book(symbol, limit=10)
        levels = book.get("asks" if side == "buy" else "bids", [])
        if not levels:
            raise RuntimeError(f"No {side} liquidity available on {exchange_name}.")

        best_price = float(levels[0][0])
        slippage = MAX_SLIPPAGE_PCT / 100
        if side == "buy" and best_price > reference_price * (1 + slippage):
            raise RuntimeError(f"Buy price moved beyond {MAX_SLIPPAGE_PCT:.2f}% on {exchange_name}.")
        if side == "sell" and best_price < reference_price * (1 - slippage):
            raise RuntimeError(f"Sell price moved beyond {MAX_SLIPPAGE_PCT:.2f}% on {exchange_name}.")

        available = sum(float(level[1]) for level in levels)
        if available < quantity:
            raise RuntimeError(f"Insufficient {side} liquidity on {exchange_name}.")
        return best_price

    def execute_arbitrage(self, buy_exchange, sell_exchange, symbol,
                          amount_usdt, buy_price, sell_price):
        """Preflight balances, then submit both live market orders."""
        if not REAL_TRADING_ENABLED or EXECUTION_MODE != "real":
            raise RuntimeError("Real execution is not explicitly enabled.")

        buy_fee = self.taker_fee(buy_exchange, symbol)
        sell_fee = self.taker_fee(sell_exchange, symbol)
        base = base_coin(symbol)
        estimated_quantity = (amount_usdt / buy_price) * (1.0 - buy_fee)
        if self.get_balance_usdt(buy_exchange) < amount_usdt:
            raise RuntimeError(f"Insufficient USDT balance on {buy_exchange}.")
        if self.get_balance(sell_exchange, base) < estimated_quantity:
            raise RuntimeError(f"Insufficient {base} balance on {sell_exchange}.")
        live_buy_price = self.check_order_book(
            buy_exchange, symbol, "buy", estimated_quantity, buy_price)
        live_sell_price = self.check_order_book(
            sell_exchange, symbol, "sell", estimated_quantity, sell_price)
        conservative_profit_pct = (
            (live_sell_price / live_buy_price) * (1 - buy_fee) * (1 - sell_fee) - 1
        ) * 100
        if conservative_profit_pct < MIN_PROFIT_PCT:
            raise RuntimeError(
                f"Live profit dropped to {conservative_profit_pct:.3f}%, "
                f"below the {MIN_PROFIT_PCT:.3f}% minimum."
            )

        buy_order = self.place_market_buy(buy_exchange, symbol, amount_usdt, buy_fee)
        filled_quantity = float(buy_order.get("filled") or buy_order.get("amount") or 0.0)
        if filled_quantity <= 0:
            raise RuntimeError("Buy order returned no filled quantity.")

        try:
            sell_order = self.place_market_sell(sell_exchange, symbol, filled_quantity)
        except Exception as exc:
            raise UnhedgedPositionError(
                buy_exchange, sell_exchange, symbol, filled_quantity,
                buy_order, exc, recovery_exchange=buy_exchange,
            ) from exc
        filled_sell = float(
            sell_order.get("filled") or sell_order.get("amount") or 0.0)
        if filled_sell < filled_quantity:
            raise UnhedgedPositionError(
                buy_exchange, sell_exchange, symbol,
                max(0.0, filled_quantity - filled_sell), buy_order,
                RuntimeError("Sell leg was partially filled."),
                recovery_exchange=sell_exchange,
            )
        buy_cost = float(buy_order.get("cost") or amount_usdt)
        sell_proceeds = float(sell_order.get("cost") or (filled_quantity * sell_price))
        return {
            "buy_order": buy_order,
            "sell_order": sell_order,
            "filled_quantity": filled_quantity,
            "profit_usdt": sell_proceeds - buy_cost,
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

        btc_order = self.place_market_buy(exchange_name, first_symbol, start_usdt, fee)
        filled_btc = float(btc_order.get("filled") or btc_order.get("amount") or 0.0)
        if filled_btc <= 0:
            raise RuntimeError(f"{first_symbol} buy order returned no filled quantity.")
        requested_btc = self.normalize_amount(
            exchange_name, first_symbol, btc_quantity)
        if filled_btc < requested_btc:
            raise UnhedgedPositionError(
                exchange_name, exchange_name, first_symbol, filled_btc, btc_order,
                RuntimeError("First triangular leg was partially filled."),
            )

        try:
            requested_eth = self.normalize_amount(
                exchange_name, middle_symbol, filled_btc / middle_price * (1 - fee))
            eth_order = self.place_market_buy_quantity(
                exchange_name, middle_symbol, requested_eth)
        except Exception as exc:
            raise UnhedgedPositionError(
                exchange_name, exchange_name, first_symbol, filled_btc, btc_order, exc
            ) from exc
        filled_eth = float(eth_order.get("filled") or eth_order.get("amount") or 0.0)
        if filled_eth <= 0:
            raise UnhedgedPositionError(
                exchange_name, exchange_name, first_symbol, filled_btc, btc_order,
                RuntimeError(f"{middle_symbol} buy order returned no filled quantity."),
            )
        if filled_eth < requested_eth:
            residual_first = max(
                0.0, filled_btc - (filled_eth * middle_price / (1 - fee)))
            raise UnhedgedPositionError(
                exchange_name, exchange_name, first_symbol, residual_first, btc_order,
                RuntimeError("Middle triangular leg was partially filled."),
            )

        try:
            eth_sell_order = self.place_market_sell(
                exchange_name, final_symbol, filled_eth)
        except Exception as exc:
            raise UnhedgedPositionError(
                exchange_name, exchange_name, final_symbol, filled_eth, eth_order, exc
            ) from exc
        filled_sell = float(
            eth_sell_order.get("filled") or eth_sell_order.get("amount") or 0.0)
        if filled_sell < filled_eth:
            raise UnhedgedPositionError(
                exchange_name, exchange_name, final_symbol,
                max(0.0, filled_eth - filled_sell), eth_order,
                RuntimeError("Final triangular leg was partially filled."),
            )

        start_cost = float(btc_order.get("cost") or start_usdt)
        proceeds = float(eth_sell_order.get("cost") or (filled_eth * final_price))
        return {
            "buy_order": btc_order,
            "middle_order": eth_order,
            "sell_order": eth_sell_order,
            "profit_usdt": proceeds - start_cost,
        }


# ==================================================================
#  PAPER WALLET  —  virtual balances, one per exchange per symbol
# ==================================================================

def base_coin(symbol):
    """'BTC/USDT' -> 'BTC'"""
    return symbol.split("/")[0]


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
        if TRADING_STRATEGY == "triangular":
            self.routes = discover_triangular_routes(self.markets[next(iter(self.markets))])
        route_symbols = [symbol for route in self.routes for symbol in route["symbols"]]
        self.symbols = list(dict.fromkeys(route_symbols or (symbols or SYMBOLS)))

    def get_quotes(self):
        quotes = {}
        for name, client in self.clients.items():
            for sym in self.symbols:
                try:
                    t = client.fetch_ticker(sym)
                    if t["bid"] and t["ask"]:
                        quotes.setdefault(sym, {})[name] = {
                            "bid": t["bid"],
                            "ask": t["ask"],
                        }
                except Exception:
                    pass  # symbol not on this exchange / hiccup -> skip
        return quotes


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

    # Profit math: buy 1 coin costs `ask` (+fee), selling gives `bid` (-fee)
    gross_pct = (bid - ask) / ask * 100
    net_pct = gross_pct - (2 * TAKER_FEE * 100)  # fee on both sides

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

def init_csv():
    with open(LOG_FILE, "w", newline="") as f:
        csv.writer(f).writerow([
            "time", "symbol", "buy_exchange", "sell_exchange",
            "buy_price", "sell_price", "trade_size_usdt",
            "profit_usdt", "net_profit_pct", "buy_order_id",
            "sell_order_id", "status", "middle_order_id",
        ])


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
