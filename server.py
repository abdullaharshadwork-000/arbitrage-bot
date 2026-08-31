#!/usr/bin/env python3
"""
=====================================================================
 ARBITRAGE BOT — WEB SERVER
=====================================================================
Wraps the classes and functions already defined in arbitrage_bot.py
(PaperWallet, DemoFeed, LiveFeed, find_opportunity, log_trade, ...)
in a small Flask app so the dashboard (arbitrage-bot-terminal.html)
can start/stop/configure the bot and read its live state over HTTP,
instead of the browser faking everything in JavaScript.

Nothing about arbitrage_bot.py's own logic is changed or duplicated —
this file just drives it and exposes its state as JSON.

RUN:
    pip install flask ccxt
    python3 server.py
Then open:
    http://localhost:5000
=====================================================================
"""

import contextlib
import os
import secrets
import socket
import threading
import time
import math
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, Response

import arbitrage_bot as bot
from arbicore import alerts, config as arbiconfig, reconcile, risk

app = Flask(__name__, static_folder=".", static_url_path="")
# Overridable so a test run - or a second instance - never writes into the
# operator's real trade history.
DB_FILE = Path(os.environ.get("ARBICORE_DB") or Path(__file__).with_name("arbicore.db"))

# ------------------------------------------------------------------
#  LOCAL ACCESS GUARD
# ------------------------------------------------------------------
# Binding to 127.0.0.1 keeps the network out, but it does not keep other pages
# in the same browser out: any site the operator has open can POST to
# http://localhost:5000/api/config, and one of the things that endpoint sets is
# real_trading_enabled. Nothing here is a substitute for real authentication -
# it is the minimum that makes a mutating request prove it came from the
# dashboard this server served:
#
#   * a token, delivered as a SameSite=Strict cookie, which browsers refuse to
#     attach to any cross-site request, and accepted in a header as well so
#     curl and scripts still work;
#   * an Origin check against this server's own origin. SameSite treats every
#     port on localhost as the same site, so another local dev server the
#     operator visits would otherwise be able to ride the cookie; comparing the
#     full origin (scheme, host and port) is what closes that.
#
# Set ARBICORE_TOKEN to pin the token across restarts; otherwise it rotates on
# every start, which invalidates any script that hard-coded the old one.
API_TOKEN = os.environ.get("ARBICORE_TOKEN") or secrets.token_urlsafe(24)
TOKEN_COOKIE = "arbicore_token"
TOKEN_HEADER = "X-Arbicore-Token"
READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def origin_is_this_server(origin):
    """False for any Origin that is not exactly this server.

    A missing Origin is allowed: curl and the requests library do not send one,
    while a browser always does on the cross-site requests this is guarding
    against. `request.host_url` comes from the Host header, which a page cannot
    forge - the browser fills it in from the URL it is actually calling.
    """
    if not origin:
        return True
    return origin.rstrip("/").lower() == request.host_url.rstrip("/").lower()


def presented_token():
    return request.headers.get(TOKEN_HEADER) or request.cookies.get(TOKEN_COOKIE)


@app.before_request
def guard_mutating_requests():
    """Every state-changing route, in one place, so a new route is covered by
    default rather than by remembering to decorate it."""
    if request.method in READ_ONLY_METHODS:
        return None
    if not origin_is_this_server(request.headers.get("Origin")):
        return jsonify({
            "ok": False,
            "error": ("Refused: this request came from another page. Open the "
                      "dashboard at http://localhost:5000 and use it there."),
        }), 403
    token = presented_token()
    if not token or not secrets.compare_digest(token, API_TOKEN):
        return jsonify({
            "ok": False,
            "error": ("Refused: no valid session token. Load the dashboard from "
                      "this server (http://localhost:5000) rather than opening "
                      f"the HTML file directly, or send the {TOKEN_HEADER} "
                      "header printed at startup."),
        }), 403
    return None


@app.after_request
def issue_token_cookie(response):
    """Hand the token to anything this server serves.

    Done for every page rather than only for "/" because the dashboard can be
    opened at /arbitrage-bot-terminal.html too, and a page without the cookie is
    a dashboard whose buttons all fail.

    Only on reads, and only on ones that succeeded: a rejected POST must not
    answer with the very credential it was rejected for lacking.
    """
    if (request.method in READ_ONLY_METHODS
            and response.status_code < 400
            and request.cookies.get(TOKEN_COOKIE) != API_TOKEN):
        response.set_cookie(TOKEN_COOKIE, API_TOKEN, samesite="Strict",
                            httponly=True, path="/")
    return response


# ------------------------------------------------------------------
#  SHARED STATE  (guarded by state_lock — read/written from the
#  background scan loop AND from Flask request handlers)
# ------------------------------------------------------------------

state_lock = threading.Lock()

state = {
    "running": False,
    "scan_count": 0,
    "trades_count": 0,
    "attempts_count": 0,
    "total_profit": 0.0,
    "portfolio_value": 0.0,
    "start_value": 0.0,
    "portfolio_source": "paper_wallet",
    "quotes": {},         # quotes[symbol][exchange] = {bid, ask}
    "mid_prices": {},
    "chart_series": [],
    "chart_label": "Market price",
    "latest_cycle": None,
    "triangular_routes": [],
    "recent": [],          # blotter events, newest first (hits + misses)
    "trades": [],           # full trade log this session, newest first
    "unhedged_positions": [],
    "balances": {},
    "balance_valuation": {"free_usdt": 0.0, "used_usdt": 0.0, "total_usdt": 0.0},
    "exchange_status": [],
    "config": {
        "mode": bot.MODE,               # "demo" | "live"
        "execution_mode": bot.EXECUTION_MODE,  # "paper" | "real"
        "strategy": bot.TRADING_STRATEGY,
        "real_trading_enabled": bot.REAL_TRADING_ENABLED,
        "trade_size": bot.TRADE_SIZE_USDT,
        "fee": bot.TAKER_FEE,
        "min_profit": bot.MIN_PROFIT_PCT,
        "max_slippage": bot.MAX_SLIPPAGE_PCT,
        "interval": bot.CHECK_INTERVAL,
        "gap_chance": bot.DEMO_GAP_CHANCE,
        # Risk limits. Each one latches the loop when it trips, so they are
        # config, not decoration.
        "max_daily_loss": bot.MAX_DAILY_LOSS_USDT,
        "max_position_notional": bot.MAX_POSITION_NOTIONAL_USDT,
        "max_consecutive_failures": bot.MAX_CONSECUTIVE_FAILURES,
        "max_orders_per_minute": bot.MAX_ORDERS_PER_MINUTE,
    },
    "active_exchanges": list(bot.EXCHANGES),
    "active_symbols": list(bot.SYMBOLS),
    "risk": {},
    "alerts": {},
    "startup_check": None,
    "feed_health": {},
    "error": None,
}


@contextlib.contextmanager
def db():
    """A committed and then closed connection.

    `with sqlite3.connect(...)` commits the transaction but does not close the
    connection, so every helper below leaked a handle - visible as a
    ResourceWarning under the test suite, and as a slowly climbing descriptor
    count in a server that is meant to run for weeks.
    """
    connection = sqlite3.connect(DB_FILE, timeout=10.0)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


# Columns added after the first release. Existing rows keep NULL for them,
# which is honest: those trades really were recorded without the field.
TRADE_COLUMNS = {
    "buy_price": "REAL",
    "sell_price": "REAL",
    "execution_mode": "TEXT",
    "strategy": "TEXT",
    "filled_quantity": "REAL",
    "unsold_dust": "REAL",
}


def _add_missing_columns(connection, table, columns):
    """Bring one table up to the current schema without touching its rows.

    The names come from the literal dict above, never from a request, so the
    interpolation here cannot be driven by an operator or an exchange.
    """
    existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    for name, kind in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")


def initialize_database():
    with db() as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                time TEXT NOT NULL,
                symbol TEXT NOT NULL,
                buy_exchange TEXT,
                sell_exchange TEXT,
                trade_size_usdt REAL,
                profit_usdt REAL,
                net_profit_pct REAL,
                buy_order_id TEXT,
                middle_order_id TEXT,
                sell_order_id TEXT,
                status TEXT
            );
            CREATE TABLE IF NOT EXISTS recovery_positions (
                buy_order_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS balance_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS trades_time ON trades (time);
        """)
        _add_missing_columns(connection, "trades", TRADE_COLUMNS)


def persist_trade(trade):
    with db() as connection:
        connection.execute(
            """INSERT INTO trades
            (time, symbol, buy_exchange, sell_exchange, trade_size_usdt,
             profit_usdt, net_profit_pct, buy_order_id, middle_order_id,
             sell_order_id, status, buy_price, sell_price, execution_mode,
             strategy, filled_quantity, unsold_dust)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (trade["time"], trade["symbol"], trade["buy_exchange"],
             trade["sell_exchange"], trade["trade_size_usdt"],
             trade["profit_usdt"], trade["net_profit_pct"],
             trade.get("buy_order_id"), trade.get("middle_order_id"),
             trade.get("sell_order_id"), trade.get("status"),
             trade.get("buy_price"), trade.get("sell_price"),
             trade.get("execution_mode"), trade.get("strategy"),
             trade.get("filled_quantity"), trade.get("unsold_dust")),
        )


def persist_recovery(position):
    with db() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO recovery_positions (buy_order_id, created_at, payload) VALUES (?, ?, ?)",
            (position.get("buy_order_id") or position["time"],
             position["time"], json.dumps(position)),
        )


def remove_persisted_recovery(order_id):
    with db() as connection:
        connection.execute("DELETE FROM recovery_positions WHERE buy_order_id = ?", (order_id,))


def persist_balances(balances, valuation):
    payload = {"balances": balances, "valuation": valuation}
    with db() as connection:
        connection.execute(
            "INSERT INTO balance_snapshots (created_at, payload) VALUES (?, ?)",
            (datetime.now().isoformat(timespec="seconds"), json.dumps(payload)),
        )


initialize_database()


def load_persisted_recovery():
    with db() as connection:
        rows = connection.execute(
            "SELECT payload FROM recovery_positions ORDER BY created_at DESC"
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


state["unhedged_positions"] = load_persisted_recovery()

wallet = None
feed = None
real_engine = None
active_symbols = []

_thread = None
_stop_flag = threading.Event()
_emergency_stop = threading.Event()

MAX_RECENT = 60
MAX_TRADES = 500
MAX_BLOCKED_SYMBOLS = 5       # named pairs on a collapsed veto row
BLOCKED_SYMBOL_OVERFLOW = "..."
STOP_JOIN_TIMEOUT = 25.0   # a two-leg real trade can legitimately take this
                           # long to reconcile; a rebuild must wait it out

# Config keys whose change can rebuild the feed, wallet or exchange clients.
# A rebuild is only safe with the scan thread stopped and joined.
REBUILD_KEYS = ("mode", "execution_mode", "strategy", "real_trading_enabled",
                "exchanges", "symbols")

# Risk limits and alerts are process-wide, not per-engine-build: a daily loss
# does not stop counting because the operator switched symbols.
notifier = alerts.Notifier(
    webhook_url=getattr(bot, "ALERT_WEBHOOK", ""),
    min_severity=alerts.WARNING)
risk_manager = risk.RiskManager(arbiconfig.Settings())


# Dashboard field name -> the Settings field holding the same quantity. The
# short names are baked into the HTML and into the published state, so the
# translation lives here rather than being forced on either side.
#
# `interval` and `gap_chance` are deliberately absent. The engine sleeps a float
# number of seconds and the dashboard's slider offers 0.5s steps, while
# Settings.check_interval is a whole number of seconds; and gap_chance only
# exists for the demo price generator, which Settings does not model at all.
# Both keep their own rule in `validate_config_update`.
CONFIG_TO_SETTINGS = {
    "trade_size": "trade_size_usdt",
    "fee": "taker_fee",
    "min_profit": "min_profit_pct",
    "max_slippage": "max_slippage_pct",
    "max_daily_loss": "max_daily_loss_usdt",
    "max_position_notional": "max_position_notional_usdt",
    "max_consecutive_failures": "max_consecutive_failures",
    "max_orders_per_minute": "max_orders_per_minute",
    "mode": "mode",
    "strategy": "strategy",
}


def proposed_settings(data=None):
    """The configuration this update would produce, as one Settings object.

    Reads the published config, which is only written under `state_lock`, so
    each field is a whole value; two concurrent config posts are the operator
    racing themselves, and both are validated.

    Checking incoming fields one at a time cannot see the rules that involve
    more than one of them, and those are the rules that matter: the hard cap on
    trade size, the minimum notional every venue enforces, size against the
    position limit, a cross-exchange strategy left with a single venue. The
    dashboard used to accept a trade size larger than the position cap and then
    veto every candidate of every scan for it - a valid config change according
    to each individual check, and a bot that could not trade.

    Execution mode is pinned to paper here on purpose. The real-money gates have
    their own refusal with a better message, and selecting "real" in the
    dashboard is how the operator gets the readiness panel to tell them what is
    still missing.
    """
    incoming = data or {}
    cfg = state["config"]
    values = {}
    for key, field_name in CONFIG_TO_SETTINGS.items():
        value = incoming.get(key, cfg.get(key))
        if value is not None:
            values[field_name] = value
    # The same filtering the route applies, so validation judges the list that
    # would actually be installed rather than the one that was asked for.
    exchanges = incoming.get("exchanges", state["active_exchanges"])
    values["exchanges"] = tuple(name for name in exchanges
                                if name in bot.EXCHANGES_MASTER)
    symbols = incoming.get("symbols", state["active_symbols"])
    values["symbols"] = tuple(name for name in symbols
                              if name in bot.SYMBOLS_MASTER)
    values["min_notional_usdt"] = getattr(bot, "MIN_NOTIONAL_USDT", 10.0)
    values["execution_mode"] = arbiconfig.EXECUTION_PAPER
    return arbiconfig.Settings(**values)


def risk_settings():
    """A Settings snapshot carrying the operator's current numeric limits.

    RiskManager reads its thresholds off Settings, so rebuilding this on a
    config change is what makes a new trade size or loss cap take effect. It is
    the same translation the validator uses - two copies of the mapping is how
    a limit ends up enforced at one value and validated against another.
    """
    return proposed_settings()


def apply_risk_settings():
    """Point the live RiskManager at the current limits, keeping its tallies."""
    risk_manager.settings = risk_settings()
    return risk_manager.settings


def stop_scan_thread(timeout=STOP_JOIN_TIMEOUT):
    """Stop the loop and wait for it to actually leave.

    Clearing `state["running"]` was never enough: the loop only tests the flag
    between scans, so a rebuild triggered right after the flag was set could
    swap `feed` and `wallet` out from underneath an order that was still being
    reconciled. Returns False if the thread is still working, and the caller
    must then refuse to rebuild rather than race it.
    """
    global _thread
    _stop_flag.set()
    thread = _thread
    if thread is None or thread is threading.current_thread():
        return True
    thread.join(timeout)
    if thread.is_alive():
        return False
    _thread = None
    return True


def validate_config_update(data):
    """Return an error message when a dashboard setting is unsafe."""
    limits = {
        "trade_size": lambda value: value > 0,
        "fee": lambda value: 0 < value < 0.05,
        "min_profit": lambda value: value > 0,
        "max_slippage": lambda value: 0 < value <= 5,
        "interval": lambda value: value > 0,
        "gap_chance": lambda value: 0 <= value <= 1,
        "max_daily_loss": lambda value: value > 0,
        "max_position_notional": lambda value: value > 0,
        "max_consecutive_failures": lambda value: value >= 1,
        "max_orders_per_minute": lambda value: value >= 1,
    }
    for key, is_valid in limits.items():
        if key not in data:
            continue
        try:
            value = float(data[key])
        except (TypeError, ValueError):
            return f"{key} must be a number."
        if not math.isfinite(value) or not is_valid(value):
            return f"{key} has an invalid value."
    # Then the same values as one configuration, so the cross-field rules apply.
    try:
        problems = proposed_settings(data).validate()
    except (KeyError, TypeError, ValueError) as exc:
        return str(exc)
    return problems[0] if problems else None


def get_readiness():
    """Return safe, non-secret startup status for the dashboard."""
    if state["config"]["execution_mode"] != "real":
        return {"ready": False, "message": "Paper execution is selected."}
    if not state["config"]["real_trading_enabled"]:
        return {"ready": False, "message": "Real trading is not explicitly enabled."}
    validation = bot.validate_real_trading_config()
    return {"ready": validation["ok"], "message": validation["message"]}


def collect_live_balances(engine=None, exchanges=None, symbols=None):
    """The network half of a balance refresh. Touches no shared state.

    Split out from `refresh_live_balances` so the scan loop can do the fetching
    - one request per venue, seconds of latency - without holding `state_lock`
    and blocking every dashboard poll and the emergency stop behind it.

    Returns (balances, valuation), or (None, None) with no real engine. Never
    exposes API credentials: only the currencies actually traded are read back.
    """
    engine = engine or real_engine
    if not engine:
        return None, None
    currencies = {"USDT"}
    for symbol in (symbols if symbols is not None else active_symbols):
        currencies.add(bot.base_coin(symbol))
    refreshed = {}
    valuation = {"free_usdt": 0.0, "used_usdt": 0.0, "total_usdt": 0.0}
    for exchange in (exchanges if exchanges is not None else state["active_exchanges"]):
        try:
            balances = engine.fetch_balances(exchange, sorted(currencies))
            refreshed[exchange] = balances
            values = engine.value_balances_usdt(exchange, balances)
            for key in valuation:
                valuation[key] += values[key]
        except Exception as exc:
            refreshed[exchange] = {"error": str(exc)}
    return refreshed, valuation


def publish_live_balances(refreshed, valuation):
    """The state half. Call with `state_lock` held."""
    state["balances"] = refreshed
    state["balance_valuation"] = valuation
    state["portfolio_value"] = valuation["total_usdt"]


def refresh_live_balances():
    """Fetch and publish account balances. Call with `state_lock` held."""
    refreshed, valuation = collect_live_balances()
    if refreshed is None:
        return
    publish_live_balances(refreshed, valuation)
    persist_balances(refreshed, valuation)


# ------------------------------------------------------------------
#  ENGINE  (thin wrapper around arbitrage_bot.py's own classes)
# ------------------------------------------------------------------

def init_engine():
    """(Re)builds the feed + paper wallet from the current config.
    Must be called with state_lock held."""
    global wallet, feed, real_engine, active_symbols

    mode = state["config"]["mode"]
    execution_mode = state["config"]["execution_mode"]
    strategy = state["config"]["strategy"]
    exchanges = state["active_exchanges"]
    symbols = state["active_symbols"]

    # Keep the underlying bot globals synchronized with the UI/backend
    # settings so changes to Execution Settings immediately affect the
    # engine and not just the JSON payload shown in the browser.
    bot.MODE = mode
    bot.EXECUTION_MODE = execution_mode
    bot.TRADING_STRATEGY = state["config"]["strategy"]
    bot.EXCHANGES = list(exchanges)
    bot.SYMBOLS = list(symbols)
    bot.TRADE_SIZE_USDT = state["config"]["trade_size"]
    bot.TAKER_FEE = state["config"]["fee"]
    bot.MIN_PROFIT_PCT = state["config"]["min_profit"]
    bot.MAX_SLIPPAGE_PCT = state["config"]["max_slippage"]
    bot.CHECK_INTERVAL = state["config"]["interval"]
    bot.DEMO_GAP_CHANCE = state["config"]["gap_chance"]
    bot.REAL_TRADING_ENABLED = bool(state["config"].get("real_trading_enabled", bot.REAL_TRADING_ENABLED))

    if execution_mode == "real":
        if mode != "live" or not bot.REAL_TRADING_ENABLED:
            raise ValueError("Real execution requires live mode and explicit real-trading enablement.")
        validation = bot.validate_real_trading_config()
        if not validation["ok"]:
            raise ValueError(validation["message"])
        real_engine = bot.RealExecutionEngine(exchanges)
    else:
        real_engine = None

    apply_risk_settings()

    if strategy == "triangular" and mode != "live":
        raise ValueError("Triangular strategy currently requires live market data.")
    engine_symbols = ["BTC/USDT", "ETH/BTC", "ETH/USDT"] if strategy == "triangular" else symbols
    routes = []
    bot.SYMBOLS = engine_symbols

    try:
        if mode == "live":
            feed = bot.LiveFeed(exchanges, engine_symbols)
            if strategy == "triangular":
                routes = feed.routes
                engine_symbols = list(dict.fromkeys(
                    symbol for route in routes for symbol in route["symbols"]))
                feed.symbols = engine_symbols
            first = feed.get_quotes()
            start_prices = {}
            for sym in engine_symbols:
                if sym in first and first[sym]:
                    prices = [q["bid"] for q in first[sym].values()]
                    start_prices[sym] = sum(prices) / len(prices)
        else:
            feed = bot.DemoFeed(exchanges, symbols)
            start_prices = {s: bot.DEMO_START_PRICES[s] for s in symbols
                             if s in bot.DEMO_START_PRICES}

        active_symbols = [s for s in engine_symbols if s in start_prices]
        state["triangular_routes"] = routes
        state["config"]["triangular_routes"] = routes
        state["feed_health"] = feed_health()
        wallet = bot.PaperWallet(exchanges, active_symbols,
                                  bot.START_CASH_PER_EXCHANGE, start_prices)
        if execution_mode == "real":
            refresh_live_balances()
            state["start_value"] = None
            state["portfolio_value"] = None
            state["portfolio_source"] = "exchange_balances"
            run_startup_check(active_symbols, start_prices)
        else:
            state["startup_check"] = None
            state["start_value"] = wallet.total_value(start_prices)
            state["portfolio_value"] = state["start_value"]
            state["portfolio_source"] = "paper_wallet"
        state["error"] = None
    except Exception as e:
        state["error"] = f"engine init failed: {e}"
        raise


def feed_health():
    """What the last quote pass actually managed to price."""
    if feed is None:
        return {}
    return {
        "symbols": len(getattr(feed, "symbols", []) or []),
        "routes": len(getattr(feed, "routes", []) or []),
        "route_candidates": getattr(feed, "route_candidates", 0),
        "fetch_seconds": round(getattr(feed, "last_fetch_seconds", 0.0), 3),
        "rejected": {name: len(reasons) for name, reasons
                     in (getattr(feed, "rejected_quotes", {}) or {}).items()},
    }


def run_startup_check(symbols, prices):
    """Refuse to start real trading on top of something left over.

    A resting order, a stranded balance or a skewed clock all mean the account
    is not in the state the bot thinks it is. Starting anyway is how a restart
    turns one unresolved position into two. Blocking findings halt the risk
    manager, which requires an operator to clear them.
    """
    if not real_engine:
        state["startup_check"] = None
        return None

    expected = {}
    for exchange, balances in (state["balances"] or {}).items():
        if isinstance(balances, dict) and "error" not in balances:
            expected[exchange] = {
                currency: entry.get("free", 0.0) if isinstance(entry, dict) else entry
                for currency, entry in balances.items()}

    report = reconcile.startup_check(
        real_engine.clients, symbols,
        expected_balances=None,          # drift needs a prior snapshot to mean
                                         # anything; the first run has none
        prices=prices,
        pending_records=[p for p in state["unhedged_positions"]
                         if p.get("status") == "manual_recovery_required"],
        max_clock_skew_ms=getattr(bot, "MAX_CLOCK_SKEW_MS", 2000))
    payload = report.as_dict()
    state["startup_check"] = payload
    if report.blocking:
        summary = report.summary()
        risk_manager.halt(risk.HALT_RECONCILE, summary)
        notifier.send("Startup check blocked real trading", summary,
                      severity=alerts.CRITICAL, fingerprint="startup_check")
        state["error"] = f"Startup check blocked real trading: {summary}"
    return payload


def reset_state():
    _emergency_stop.clear()
    with state_lock:
        state["scan_count"] = 0
        state["trades_count"] = 0
        state["attempts_count"] = 0
        state["total_profit"] = 0.0
        state["recent"] = []
        state["trades"] = []
        state["unhedged_positions"] = []
        state["balances"] = {}
        state["balance_valuation"] = {"free_usdt": 0.0, "used_usdt": 0.0, "total_usdt": 0.0}
        state["chart_series"] = []
        state["latest_cycle"] = None
        state["triangular_routes"] = []
        init_engine()
    bot.init_csv()  # ensure the header exists; never truncates the log


# ------------------------------------------------------------------
#  SCAN LOOP
# ------------------------------------------------------------------
# One rule shapes everything below: no network call and no order placement
# happens while `state_lock` is held. The old loop held the lock for an entire
# scan, so a two-leg real trade - which can spend 25 seconds placing and
# polling orders - blocked /api/state and /api/emergency-stop for that whole
# time. An emergency stop that has to wait for the order it is trying to stop
# is not an emergency stop.
#
# The lock is now taken in short bursts: to snapshot the config, to publish the
# scan counters, and once per trade to publish its result.


def find_candidates(cfg, quotes, symbols, exchanges, scan_fee):
    """Everything worth trading in this snapshot, plus the attempt count.

    Pure computation over quotes that were already fetched, so it holds no lock
    and waits on nothing. The triangular cycle is computed once here; the old
    loop computed it twice per scan - once for the chart, once to trade - so a
    cycle that changed between the two reads was traded at numbers the chart
    never showed.
    """
    attempts = 0
    candidates = []
    cycle = None

    if cfg["strategy"] == "triangular":
        # A cycle counts as one attempt per scan whether or not it prices up:
        # the dashboard's hit rate is per scan, not per leg.
        attempts = 1
        cycle = bot.find_best_triangular_opportunity(
            quotes, exchanges[0], cfg["trade_size"], scan_fee,
            cfg.get("triangular_routes", []))
        if cycle:
            candidates.append({
                "symbol": "triangular",
                "buy_exchange": exchanges[0],
                "sell_exchange": exchanges[0],
                "ask": cycle["prices"][cycle["symbols"][0]],
                "bid": cycle["prices"][cycle["symbols"][2]],
                "net_pct": cycle["profit_pct"],
                "cycle": cycle,
                "legs": 3,
            })
        return candidates, attempts, cycle

    for symbol in symbols:
        if symbol not in quotes or len(quotes[symbol]) < 2:
            continue
        attempts += 1
        opportunity = bot.find_opportunity(quotes[symbol])
        if not opportunity:
            continue
        buy_ex, sell_ex, ask, bid, net_pct = opportunity
        candidates.append({
            "symbol": symbol, "buy_exchange": buy_ex, "sell_exchange": sell_ex,
            "ask": ask, "bid": bid, "net_pct": net_pct, "cycle": None,
            "legs": 2,
        })
    return candidates, attempts, cycle


def execute_candidate(cfg, candidate, engine, paper):
    """Place one opportunity's orders. Called with no lock held.

    Returns (usdt_out, result) where `result` is the engine's own report, or
    (None, None) when the paper wallet declines for lack of balance - a refusal,
    not a failure, since nothing was risked. Exceptions are left to propagate:
    the caller decides whether they stop the loop.
    """
    size = cfg["trade_size"]
    real = cfg["execution_mode"] == "real"

    if cfg["strategy"] == "triangular":
        cycle = candidate["cycle"]
        if real:
            result = engine.execute_triangular(
                candidate["buy_exchange"], size, cycle["prices"], cycle["symbols"])
            return size + result["profit_usdt"], result
        # Paper triangular books the modelled return; there are no orders, and
        # inventing ids for them would put fiction in the trade log.
        return size + cycle["profit_usdt"], {}

    if real:
        result = engine.execute_arbitrage(
            candidate["buy_exchange"], candidate["sell_exchange"],
            candidate["symbol"], size, candidate["ask"], candidate["bid"])
        return size + result["profit_usdt"], result

    booked = paper.execute_arbitrage(
        candidate["buy_exchange"], candidate["sell_exchange"], candidate["symbol"],
        size, candidate["ask"], candidate["bid"], cfg["fee"])
    if booked is None:
        return None, None
    return booked[1], {}


def build_trade_record(cfg, candidate, result, profit, now):
    """The row the dashboard, the database and trades.csv all read.

    Order ids come out of the engine's report and are only read in real mode:
    paper execution has no orders, and a fabricated id in the trade log is
    worse than a blank one.
    """
    real = cfg["execution_mode"] == "real"
    orders = result or {}
    return {
        "time": now.isoformat(timespec="seconds"),
        "symbol": candidate["symbol"],
        "buy_exchange": candidate["buy_exchange"],
        "sell_exchange": candidate["sell_exchange"],
        "buy_price": round(candidate["ask"], 6),
        "sell_price": round(candidate["bid"], 6),
        "trade_size_usdt": cfg["trade_size"],
        "profit_usdt": round(profit, 4),
        "net_profit_pct": round(candidate["net_pct"], 3),
        "buy_order_id": (orders.get("buy_order") or {}).get("id") if real else None,
        "sell_order_id": (orders.get("sell_order") or {}).get("id") if real else None,
        "middle_order_id": ((orders.get("middle_order") or {}).get("id")
                            if real and cfg["strategy"] == "triangular" else None),
        "execution_mode": cfg["execution_mode"],
        "strategy": cfg["strategy"],
        # Real fills differ from the request: step-size flooring leaves dust and
        # the confirmed quantity is what actually traded.
        "filled_quantity": orders.get("filled_quantity"),
        "unsold_dust": orders.get("unsold_dust"),
        "status": "filled",
    }


def _add_recent(entry):
    """Push one blotter event, newest first. Call with `state_lock` held.

    Repeated vetoes for the same reason collapse into the row already at the
    top. Once a standing limit like the order rate is reached, every candidate
    of every scan is refused identically, and one row apiece buries the trades
    the operator actually wants to read - a live run filled all 60 rows with
    the same sentence and pushed 24 completed trades out of the blotter. A
    count says the same thing in one line.
    """
    recent = state["recent"]
    if entry.get("type") == "blocked" and recent:
        top = recent[0]
        if (top.get("type") == "blocked"
                and top.get("limit") == entry.get("limit")
                and top.get("reason") == entry.get("reason")):
            top["time"] = entry["time"]
            top["count"] = top.get("count", 1) + 1
            symbols = top.setdefault("symbols", [top.get("symbol")])
            if entry.get("symbol") not in symbols:
                # Enough symbols to show the veto is not one stuck pair,
                # not so many that the row wraps.
                if len(symbols) < MAX_BLOCKED_SYMBOLS:
                    symbols.append(entry["symbol"])
                elif symbols[-1] != BLOCKED_SYMBOL_OVERFLOW:
                    symbols.append(BLOCKED_SYMBOL_OVERFLOW)
            return
    recent.insert(0, entry)
    if len(recent) > MAX_RECENT:
        state["recent"] = recent[:MAX_RECENT]


def handle_unhedged(exc, now):
    """Record a half-done trade and return the message that stops the loop.

    This is the expensive case: one leg filled and the other did not, so real
    coin is sitting on a venue the strategy never meant to hold it on. The
    quantity is only trustworthy when the exchange confirmed the fill - when it
    did not, the recorded number is an upper bound and a human has to look at
    the order before anything is sold.
    """
    confirmed = getattr(exc, "quantity_confirmed", True)
    recovery = {
        "time": now.isoformat(timespec="seconds"),
        "status": "manual_recovery_required",
        "symbol": exc.symbol,
        "buy_exchange": exc.buy_exchange,
        "sell_exchange": exc.sell_exchange,
        "recovery_exchange": exc.recovery_exchange,
        "quantity": exc.quantity,
        "quantity_confirmed": confirmed,
        "buy_order_id": (exc.buy_order or {}).get("id"),
        "error": str(exc.cause),
    }
    currency = bot.base_coin(exc.symbol)
    # Latches the risk manager: resume() refuses while a stranded position is
    # open, so the loop cannot be restarted around inventory of unknown size.
    risk_manager.record_stranded(exc.recovery_exchange, currency, exc.quantity,
                                 detail=str(exc.cause))
    if confirmed:
        notifier.stranded(exc.recovery_exchange, currency, exc.quantity,
                          str(exc.cause))
    else:
        notifier.unreconciled(exc.cause)
    with state_lock:
        state["unhedged_positions"].insert(0, recovery)
        state["unhedged_positions"] = state["unhedged_positions"][:20]
    persist_recovery(recovery)
    return str(exc)


def scan_loop():
    """One pass per interval: quote, scan, gate on risk, execute, publish."""
    while not _stop_flag.is_set() and not _emergency_stop.is_set():
        with state_lock:
            cfg = dict(state["config"])
            exchanges = list(state["active_exchanges"])
            symbols = list(active_symbols)
            # Bound to locals for the whole scan. A rebuild can only happen
            # after stop_scan_thread() has joined this thread, so these cannot
            # be swapped mid-trade, and nothing below has to re-read a global.
            quote_feed, paper, engine = feed, wallet, real_engine

        interval = cfg["interval"]
        real = cfg["execution_mode"] == "real"

        if quote_feed is None:
            with state_lock:
                state["error"] = "engine is not built; reset before starting"
            time.sleep(interval)
            continue

        try:
            quotes = quote_feed.get_quotes()
        except Exception as exc:
            with state_lock:
                state["error"] = f"feed error: {exc}"
            time.sleep(interval)
            continue

        now = datetime.now()
        mid_prices = {}
        for symbol in symbols:
            if symbol in quotes and quotes[symbol]:
                mids = [(q["bid"] + q["ask"]) / 2 for q in quotes[symbol].values()]
                mid_prices[symbol] = sum(mids) / len(mids)

        scan_fee = cfg["fee"]
        if real and engine:
            try:
                scan_fee = engine.taker_fee(exchanges[0])
            except Exception as exc:
                # The configured fee is a guess; the venue's own fee is what the
                # profit gate has to clear. Scanning on the guess is how an
                # unprofitable spread looks tradable, so skip the scan instead.
                with state_lock:
                    state["error"] = f"could not read {exchanges[0]} taker fee: {exc}"
                time.sleep(interval)
                continue
        candidates, attempts, cycle = find_candidates(
            cfg, quotes, symbols, exchanges, scan_fee)

        with state_lock:
            state["scan_count"] += 1
            scan_num = state["scan_count"]
            state["attempts_count"] += attempts
            if cfg["strategy"] == "triangular":
                if cycle:
                    state["chart_label"] = "Triangular cycle return"
                    state["latest_cycle"] = cycle
                    state["chart_series"].append(round(cycle["final_usdt"], 6))
                    state["chart_series"] = state["chart_series"][-60:]
            else:
                state["chart_label"] = "Market price"
            state["quotes"] = quotes
            state["mid_prices"] = mid_prices
            state["feed_health"] = feed_health()

        found = []
        halt_error = None
        vetoed = 0          # opportunities we found but chose not to take

        for candidate in candidates:
            decision = risk_manager.check(cfg["trade_size"], candidate["symbol"])
            if not decision:
                vetoed += 1
                risk_manager.record_rejection(decision.reason)
                with state_lock:
                    _add_recent({
                        "type": "blocked",
                        "time": now.isoformat(timespec="seconds"),
                        "symbol": candidate["symbol"],
                        "limit": decision.limit,
                        "reason": decision.reason,
                    })
                if risk_manager.halted:
                    halt_error = (f"Risk limit ({risk_manager.halt_limit}): "
                                  f"{risk_manager.halt_reason}")
                    notifier.halted(risk_manager.halt_limit,
                                    risk_manager.halt_reason,
                                    risk_manager.snapshot())
                    break
                # A per-trade veto (size, order rate) is not a halt: the next
                # symbol, or the next scan, may well be allowed.
                continue

            # One tick per leg actually sent, so the per-minute cap counts the
            # requests the venue sees rather than the opportunities we liked.
            for _ in range(candidate.get("legs", 2)):
                risk_manager.record_order()

            try:
                usdt, result = execute_candidate(cfg, candidate, engine, paper)
            except bot.UnhedgedPositionError as exc:
                halt_error = handle_unhedged(exc, now)
                break
            except Exception as exc:
                risk_manager.record_failure(str(exc))
                halt_error = f"Execution halted: {exc}"
                if risk_manager.halted:
                    notifier.halted(risk_manager.halt_limit,
                                    risk_manager.halt_reason,
                                    risk_manager.snapshot())
                else:
                    notifier.send("Trade failed", str(exc), alerts.WARNING,
                                  fingerprint=f"failure:{candidate['symbol']}")
                break

            if usdt is None:
                # The paper wallet declined for lack of balance. Nothing was
                # risked, so that is a rejection, not a failure.
                vetoed += 1
                risk_manager.record_rejection("paper wallet balance too low")
                continue

            profit = usdt - cfg["trade_size"]
            trade = build_trade_record(cfg, candidate, result, profit, now)
            risk_manager.record_success(profit)
            if profit < 0:
                notifier.send(
                    f"Trade lost money on {candidate['symbol']}",
                    f"expected {candidate['net_pct']:+.3f}% net, realized "
                    f"{profit:+.4f} USDT",
                    alerts.WARNING, fingerprint=f"loss:{candidate['symbol']}")

            with state_lock:
                state["total_profit"] += profit
                state["trades_count"] += 1
                state["trades"].insert(0, trade)
                if len(state["trades"]) > MAX_TRADES:
                    state["trades"].pop()
                _add_recent({"type": "hit", **trade})
            persist_trade(trade)
            bot.log_trade([
                trade["time"], trade["symbol"], trade["buy_exchange"],
                trade["sell_exchange"], f"{candidate['ask']:.6f}",
                f"{candidate['bid']:.6f}", cfg["trade_size"], f"{profit:.4f}",
                f"{candidate['net_pct']:.3f}", trade["buy_order_id"] or "",
                trade["sell_order_id"] or "", cfg["execution_mode"],
                trade["middle_order_id"] or "",
            ])
            found.append(trade)

            if risk_manager.halted:
                # record_success() re-checks the latching limits, so a trade
                # that pushed the day past its loss cap stops the loop here
                # rather than on the next scan.
                halt_error = (f"Risk limit ({risk_manager.halt_limit}): "
                              f"{risk_manager.halt_reason}")
                notifier.halted(risk_manager.halt_limit, risk_manager.halt_reason,
                                risk_manager.snapshot())
                break

        if not found and not vetoed and halt_error is None:
            # A miss means the market offered nothing. A scan that found a
            # spread and declined it already said so with a "blocked" entry, and
            # the old loop logged a miss after a halt too, which read as "no
            # opportunity" for the very scan that had just stranded a position.
            with state_lock:
                _add_recent({
                    "type": "miss",
                    "time": now.isoformat(timespec="seconds"),
                    "scan_num": scan_num,
                })

        if mid_prices and not real and paper is not None:
            value = paper.total_value(mid_prices)
            with state_lock:
                state["portfolio_value"] = value

        if real and engine and scan_num % 5 == 0:
            refreshed, valuation = collect_live_balances(engine, exchanges, symbols)
            if refreshed is not None:
                # Only real equity feeds the drawdown limit. A paper book is
                # seeded with coin, so its value moves with the market and would
                # trip the limit on a price dip it never traded on.
                risk_manager.update_equity(valuation["total_usdt"])
                with state_lock:
                    publish_live_balances(refreshed, valuation)
                persist_balances(refreshed, valuation)

        with state_lock:
            state["risk"] = risk_manager.snapshot()
            state["alerts"] = notifier.snapshot()
            if halt_error:
                # Kept, not cleared. The old loop wiped state["error"] at the end
                # of the same scan that set it, so a bot that had just stranded a
                # position showed a stopped loop and no reason for it.
                state["error"] = halt_error
                state["running"] = False
            else:
                state["error"] = None

        if halt_error:
            _stop_flag.set()
            break

        time.sleep(interval)


# ------------------------------------------------------------------
#  ROUTES
# ------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(".", "arbitrage-bot-terminal.html")


@app.route("/api/state")
def api_state():
    with state_lock:
        payload = dict(state)
        payload["readiness"] = get_readiness()
        return jsonify(payload)


@app.route("/api/readiness")
def api_readiness():
    with state_lock:
        return jsonify(get_readiness())


@app.route("/api/balances")
def api_balances():
    with state_lock:
        return jsonify({
            "balances": state["balances"],
            "valuation": state["balance_valuation"],
        })


@app.route("/api/history")
def api_history():
    # The extra columns are appended, so the existing field order the dashboard
    # unpacks is untouched and rows written before the migration still load.
    trade_fields = (
        "time", "symbol", "buy_exchange", "sell_exchange", "trade_size_usdt",
        "profit_usdt", "net_profit_pct", "buy_order_id", "middle_order_id",
        "sell_order_id", "status", "buy_price", "sell_price", "execution_mode",
        "strategy", "filled_quantity", "unsold_dust",
    )
    with db() as connection:
        trades = connection.execute(
            f"SELECT {', '.join(trade_fields)} FROM trades "
            f"ORDER BY id DESC LIMIT 500"
        ).fetchall()
        recovery = connection.execute(
            "SELECT payload FROM recovery_positions ORDER BY created_at DESC"
        ).fetchall()
    return jsonify({
        "trades": [dict(zip(trade_fields, row)) for row in trades],
        "recovery": [json.loads(row[0]) for row in recovery],
    })


@app.route("/api/test-connection", methods=["POST"])
def api_test_connection():
    with state_lock:
        if state["config"]["execution_mode"] != "real":
            return jsonify({"ok": False, "error": "Select real execution before testing exchange access."}), 409
        validation = bot.validate_real_trading_config()
        if not validation["ok"]:
            return jsonify({"ok": False, "error": validation["message"]}), 400
        exchanges = list(state["active_exchanges"])

    try:
        engine = bot.RealExecutionEngine(exchanges)
        statuses = [engine.connection_status(exchange) for exchange in exchanges]
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502

    with state_lock:
        state["exchange_status"] = statuses
    all_ok = all(item["ok"] for item in statuses)
    return jsonify({"ok": all_ok, "exchanges": statuses}), 200 if all_ok else 502


@app.route("/api/recovery")
def api_recovery():
    with state_lock:
        return jsonify({"positions": state["unhedged_positions"]})


@app.route("/api/recovery/close", methods=["POST"])
def api_recovery_close():
    data = request.get_json(force=True) or {}
    if data.get("confirmation") != "CLOSE_UNHEDGED_POSITION":
        return jsonify({"ok": False, "error": "Explicit recovery confirmation is required."}), 400

    with state_lock:
        if state["config"]["execution_mode"] != "real" or not bot.REAL_TRADING_ENABLED:
            return jsonify({"ok": False, "error": "Manual recovery requires explicit real execution mode."}), 409
        positions = state["unhedged_positions"]
        order_id = data.get("buy_order_id")
        position = next((item for item in positions if item.get("buy_order_id") == order_id), None)
        if position is None:
            return jsonify({"ok": False, "error": "Recovery position was not found."}), 404
        if not position.get("quantity_confirmed", True):
            # The exchange never confirmed how much filled, so the recorded
            # quantity is an upper bound. Market-selling it would either fail
            # or sell coin from another position.
            return jsonify({
                "ok": False,
                "error": ("Fill size was never confirmed by the exchange. Check "
                          "the order on the exchange and close it by hand; the "
                          "recorded quantity is an upper bound only."),
            }), 409
        engine = real_engine

    try:
        close_order = engine.place_market_sell(
            position["recovery_exchange"], position["symbol"], position["quantity"])
    except Exception as exc:
        with state_lock:
            state["error"] = f"Recovery close failed: {exc}"
        return jsonify({"ok": False, "error": str(exc)}), 502

    with state_lock:
        state["unhedged_positions"] = [
            item for item in state["unhedged_positions"]
            if item.get("buy_order_id") != order_id
        ]
        remove_persisted_recovery(order_id)
        # The coin is gone, so the risk manager's stranded entry has to go with
        # it - resume() refuses while one is open, and a record left behind after
        # the position was actually closed would keep the bot halted forever.
        currency = bot.base_coin(position["symbol"])
        for index, entry in enumerate(risk_manager.stranded):
            if (entry["exchange"] == position["recovery_exchange"]
                    and entry["currency"] == currency):
                risk_manager.clear_stranded(index)
                break
        state["risk"] = risk_manager.snapshot()
        state["error"] = None
    return jsonify({"ok": True, "close_order_id": close_order.get("id")})


@app.route("/api/risk")
def api_risk():
    with state_lock:
        return jsonify({"risk": risk_manager.snapshot(),
                        "alerts": notifier.snapshot(),
                        "startup_check": state["startup_check"]})


@app.route("/api/risk/resume", methods=["POST"])
def api_risk_resume():
    """Clear a latched risk halt after a human has dealt with the cause.

    Deliberately not folded into /api/reset: resetting counters is bookkeeping,
    while resuming after a halt is a statement that the condition which stopped
    the bot is understood. The risk manager still refuses while the kill switch
    is engaged or a stranded position is open.
    """
    data = request.get_json(silent=True) or {}
    if data.get("confirmation") != "RESUME_AFTER_HALT":
        return jsonify({"ok": False,
                        "error": "Explicit resume confirmation is required."}), 400
    ok, message = risk_manager.resume()
    with state_lock:
        if ok:
            state["error"] = None
        state["risk"] = risk_manager.snapshot()
        snapshot = state["risk"]
    if ok:
        notifier.resumed()
    return jsonify({"ok": ok, "message": message, "risk": snapshot}), \
        200 if ok else 409


@app.route("/api/risk/stranded/clear", methods=["POST"])
def api_risk_clear_stranded():
    """Mark stranded inventory as unwound. Sells nothing.

    Only an operator can know the coin is actually gone: the bot cannot tell "I
    closed it" from "I stopped looking". Clearing the record without having
    closed the position is how the next run trades around inventory it does not
    know it holds, so the confirmation string is required.
    """
    data = request.get_json(silent=True) or {}
    if data.get("confirmation") != "STRANDED_POSITION_UNWOUND":
        return jsonify({
            "ok": False,
            "error": "Explicit confirmation that the position is closed is required.",
        }), 400
    index = data.get("index")
    remaining = risk_manager.clear_stranded(
        int(index) if index is not None else None)
    with state_lock:
        state["risk"] = risk_manager.snapshot()
        snapshot = state["risk"]
    return jsonify({"ok": True, "remaining": remaining, "risk": snapshot})


@app.route("/api/start", methods=["POST"])
def api_start():
    global _thread
    with state_lock:
        if _emergency_stop.is_set():
            return jsonify({
                "ok": False,
                "error": "Emergency stop engaged. Reset is required before restarting.",
            }), 409
        # A latched risk limit is not cleared by pressing Start. Saying so here
        # is clearer than starting a loop that stops itself on its first
        # candidate, which is what the risk gate would otherwise do.
        if risk_manager.halted:
            return jsonify({
                "ok": False,
                "error": (f"Risk limit ({risk_manager.halt_limit}) is latched: "
                          f"{risk_manager.halt_reason}. Clear it with "
                          f"POST /api/risk/resume once it has been dealt with."),
                "risk": risk_manager.snapshot(),
            }), 409
        if state["running"]:
            return jsonify({"ok": True})
        if wallet is None:
            init_engine()
        state["running"] = True
    _stop_flag.clear()
    _thread = threading.Thread(target=scan_loop, daemon=True)
    _thread.start()
    return jsonify({"ok": True})


@app.route("/api/pause", methods=["POST"])
def api_pause():
    """Stop the loop and wait for it to leave.

    Waiting matters: the reply is the operator's signal that no order is in
    flight any more. Reporting "paused" while a real trade is still reconciling
    is how someone closes the terminal on a half-done position.
    """
    stopped = stop_scan_thread()
    with state_lock:
        state["running"] = False
        if not stopped:
            state["error"] = ("Pause requested, but a trade is still being "
                              "reconciled. Do not close the process yet.")
    return jsonify({"ok": True, "settled": stopped,
                    "error": None if stopped else state.get("error")})


@app.route("/api/emergency-stop", methods=["POST"])
def api_emergency_stop():
    _emergency_stop.set()
    _stop_flag.set()
    with state_lock:
        state["running"] = False
        state["error"] = "Emergency stop engaged. Reset is required before restarting."
    return jsonify({"ok": True, "message": state["error"]})


@app.route("/api/reset", methods=["POST"])
def api_reset():
    # reset_state() rebuilds the feed and the paper wallet. Doing that while the
    # loop is mid-trade would swap those objects out from under an order that is
    # still being reconciled, so the rebuild waits for the thread to leave and
    # refuses rather than races it.
    settled = stop_scan_thread()
    with state_lock:
        state["running"] = False
    if not settled:
        with state_lock:
            state["error"] = ("A trade is still being reconciled; the engine was "
                              "not rebuilt. Try the reset again in a moment.")
            message = state["error"]
        return jsonify({"ok": False, "error": message}), 409
    try:
        reset_state()
    except Exception as e:
        with state_lock:
            state["error"] = str(e)
    return jsonify({"ok": True, "error": state.get("error")})


@app.route("/api/config", methods=["POST"])
def api_config():
    """Numeric knobs (fee, trade size, min profit, interval, gap chance)
    apply on the next scan immediately. Changing mode / exchanges /
    symbols rebuilds the engine, which requires the loop to be paused
    first (the frontend pauses+resets automatically for those)."""
    data = request.get_json(force=True) or {}
    validation_error = validate_config_update(data)
    if validation_error:
        return jsonify({"ok": False, "error": validation_error}), 400
    if data.get("execution_mode") not in (None, "paper", "real"):
        return jsonify({"ok": False, "error": "execution_mode must be 'paper' or 'real'."}), 400

    # Any of these can trigger a rebuild, and a rebuild must not race a trade in
    # flight. The join has to happen before state_lock is taken: the scan loop
    # needs that same lock to finish its scan, so joining while holding it would
    # deadlock the two threads against each other.
    if any(key in data for key in REBUILD_KEYS):
        if not stop_scan_thread():
            return jsonify({
                "ok": False,
                "error": ("A trade is still being reconciled; settings that "
                          "rebuild the engine cannot be applied yet."),
            }), 409
        with state_lock:
            state["running"] = False

    needs_rebuild = False
    with state_lock:
        for key in ("trade_size", "fee", "min_profit", "max_slippage", "interval",
                    "gap_chance", "max_daily_loss", "max_position_notional"):
            if key in data:
                state["config"][key] = float(data[key])
        # Counts, not amounts: a fractional "2.5 failures in a row" would never
        # compare equal to the integer the risk manager counts up.
        for key in ("max_consecutive_failures", "max_orders_per_minute"):
            if key in data:
                state["config"][key] = int(float(data[key]))

        if "mode" in data and data["mode"] in ("demo", "live") and data["mode"] != state["config"]["mode"]:
            state["config"]["mode"] = data["mode"]
            bot.MODE = state["config"]["mode"]
            needs_rebuild = True

        if "execution_mode" in data:
            if data["execution_mode"] != state["config"]["execution_mode"]:
                state["config"]["execution_mode"] = data["execution_mode"]
                bot.EXECUTION_MODE = data["execution_mode"]
                needs_rebuild = True

        if "strategy" in data:
            if data["strategy"] not in ("cross_exchange", "triangular"):
                return jsonify({"ok": False, "error": "strategy must be cross_exchange or triangular."}), 400
            if data["strategy"] != state["config"]["strategy"]:
                state["config"]["strategy"] = data["strategy"]
                bot.TRADING_STRATEGY = data["strategy"]
                needs_rebuild = True

        if "real_trading_enabled" in data:
            state["config"]["real_trading_enabled"] = bool(data["real_trading_enabled"])
            bot.REAL_TRADING_ENABLED = state["config"]["real_trading_enabled"]
            if bot.REAL_TRADING_ENABLED and state["config"]["mode"] != "live":
                state["config"]["mode"] = "live"
                bot.MODE = "live"
                needs_rebuild = True

        if "exchanges" in data:
            new_ex = [e for e in data["exchanges"] if e in bot.EXCHANGES_MASTER]
            if not new_ex:
                return jsonify({"ok": False, "error": "Select at least one exchange."}), 400
            if new_ex != state["active_exchanges"]:
                state["active_exchanges"] = new_ex
                bot.EXCHANGES = list(new_ex)
                needs_rebuild = True

        if "symbols" in data:
            new_sym = [s for s in data["symbols"] if s in bot.SYMBOLS_MASTER]
            if not new_sym:
                return jsonify({"ok": False, "error": "Select at least one asset."}), 400
            if new_sym != state["active_symbols"]:
                state["active_symbols"] = new_sym
                bot.SYMBOLS = list(new_sym)
                needs_rebuild = True

        # Apply the live engine globals immediately even when the loop is running.
        bot.TRADE_SIZE_USDT = float(state["config"]["trade_size"])
        bot.TAKER_FEE = float(state["config"]["fee"])
        bot.MIN_PROFIT_PCT = float(state["config"]["min_profit"])
        bot.MAX_SLIPPAGE_PCT = float(state["config"]["max_slippage"])
        bot.CHECK_INTERVAL = float(state["config"]["interval"])
        bot.DEMO_GAP_CHANCE = float(state["config"]["gap_chance"])
        bot.MAX_DAILY_LOSS_USDT = float(state["config"]["max_daily_loss"])
        bot.MAX_POSITION_NOTIONAL_USDT = float(state["config"]["max_position_notional"])
        bot.MAX_CONSECUTIVE_FAILURES = int(state["config"]["max_consecutive_failures"])
        bot.MAX_ORDERS_PER_MINUTE = int(state["config"]["max_orders_per_minute"])
        # New limits take effect on the next check, not on the next restart. The
        # running tallies (today's loss, the failure streak) are kept: raising a
        # limit must not erase the losses that were already booked under it.
        apply_risk_settings()
        state["risk"] = risk_manager.snapshot()

        if needs_rebuild and not state["running"]:
            try:
                init_engine()
            except Exception as e:
                state["error"] = str(e)

    return jsonify({"ok": True, "needs_rebuild": needs_rebuild, "error": state.get("error")})


@app.route("/api/trades.csv")
def api_csv():
    """Serves the REAL trades.csv written by arbitrage_bot.py's own
    log_trade()/init_csv() — the same file the CLI script produces."""
    try:
        with open(bot.LOG_FILE, "r") as f:
            content = f.read()
    except FileNotFoundError:
        content = "time,symbol,buy_exchange,sell_exchange,buy_price,sell_price,trade_size_usdt,profit_usdt,net_profit_pct\n"
    return Response(content, mimetype="text/csv",
                     headers={"Content-Disposition": "attachment; filename=trades.csv"})


HOST = "127.0.0.1"
CANDIDATE_PORTS = [5000, 5050, 5055, 8000, 8080]


def bootstrap():
    """Load private configuration and build the engine.

    Both launchers go through here. `start_live.py` used to call
    `app.run(port=5000)` itself, which skipped the port fallback and never
    printed the session token - on a machine where 5000 is reserved that is a
    real-money launcher that cannot start, and on any machine it is one that
    gives the operator no way to drive the API from a script.
    """
    bot.load_live_config_if_present()
    with state_lock:
        state["config"].update({
            "mode": bot.MODE,
            "execution_mode": bot.EXECUTION_MODE,
            "strategy": bot.TRADING_STRATEGY,
            "real_trading_enabled": bot.REAL_TRADING_ENABLED,
            "trade_size": bot.TRADE_SIZE_USDT,
            "fee": bot.TAKER_FEE,
            "min_profit": bot.MIN_PROFIT_PCT,
            "max_slippage": bot.MAX_SLIPPAGE_PCT,
            "interval": bot.CHECK_INTERVAL,
        })
        state["active_exchanges"] = list(bot.EXCHANGES)
        state["active_symbols"] = list(bot.SYMBOLS)

    # bot.SYMBOLS gets overwritten by init_engine() to match whatever
    # subset is "active" — keep an untouched master list around so the
    # frontend can always offer the full symbol set as toggle options.
    bot.SYMBOLS_MASTER = list(bot.SYMBOLS_MASTER)
    bot.EXCHANGES_MASTER = list(bot.EXCHANGES_MASTER)
    with state_lock:
        init_engine()


def pick_port(host=HOST, ports=None):
    """The first port that actually binds, or None."""
    for port in (ports or CANDIDATE_PORTS):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((host, port))
            return port
        except OSError:
            continue
        finally:
            s.close()
    return None


def serve(host=HOST, ports=None):
    """Bind and run. Call bootstrap() first."""
    # Bind to 127.0.0.1 only (not 0.0.0.0) — this is a local dev tool,
    # and binding to "all interfaces" is what commonly triggers Windows'
    # WinError 10013 ("socket forbidden by access permissions") when a
    # port is reserved (Hyper-V/WSL2 do this a lot) or a firewall rule
    # blocks it. If a port is blocked, try the next one automatically.
    candidates = list(ports or CANDIDATE_PORTS)
    chosen_port = pick_port(host, candidates)

    if chosen_port is None:
        print("=" * 64)
        print("  Could not bind to any of:", candidates)
        print("  Every candidate port is blocked on this machine.")
        print("  Try running as Administrator, or check:")
        print("    netsh interface ipv4 show excludedportrange protocol=tcp")
        print("  and pick a port outside any listed reserved range.")
        print("=" * 64)
        raise SystemExit(1)

    print("=" * 64)
    print(f"  Arbitrage bot backend running — open http://localhost:{chosen_port}")
    if chosen_port != candidates[0]:
        print(f"  (port {candidates[0]} was unavailable, used {chosen_port} instead)")
    print(f"  Session token ({TOKEN_HEADER}): {API_TOKEN}")
    print("  The dashboard picks this up on its own. It is only needed for")
    print("  curl or scripts, and it rotates on restart unless ARBICORE_TOKEN")
    print("  is set in the environment.")
    print("=" * 64)
    app.run(host=host, port=chosen_port, debug=False)


if __name__ == "__main__":
    bootstrap()
    serve()