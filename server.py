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

import threading
import time
import math
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, Response

import arbitrage_bot as bot

app = Flask(__name__, static_folder=".", static_url_path="")
DB_FILE = Path(__file__).with_name("arbicore.db")

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
    },
    "active_exchanges": list(bot.EXCHANGES),
    "active_symbols": list(bot.SYMBOLS),
    "error": None,
}


def initialize_database():
    with sqlite3.connect(DB_FILE) as connection:
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
        """)


def persist_trade(trade):
    with sqlite3.connect(DB_FILE) as connection:
        connection.execute(
            """INSERT INTO trades
            (time, symbol, buy_exchange, sell_exchange, trade_size_usdt,
             profit_usdt, net_profit_pct, buy_order_id, middle_order_id,
             sell_order_id, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (trade["time"], trade["symbol"], trade["buy_exchange"],
             trade["sell_exchange"], trade["trade_size_usdt"],
             trade["profit_usdt"], trade["net_profit_pct"],
             trade.get("buy_order_id"), trade.get("middle_order_id"),
             trade.get("sell_order_id"), trade.get("status")),
        )


def persist_recovery(position):
    with sqlite3.connect(DB_FILE) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO recovery_positions (buy_order_id, created_at, payload) VALUES (?, ?, ?)",
            (position.get("buy_order_id") or position["time"],
             position["time"], json.dumps(position)),
        )


def remove_persisted_recovery(order_id):
    with sqlite3.connect(DB_FILE) as connection:
        connection.execute("DELETE FROM recovery_positions WHERE buy_order_id = ?", (order_id,))


def persist_balances(balances, valuation):
    payload = {"balances": balances, "valuation": valuation}
    with sqlite3.connect(DB_FILE) as connection:
        connection.execute(
            "INSERT INTO balance_snapshots (created_at, payload) VALUES (?, ?)",
            (datetime.now().isoformat(timespec="seconds"), json.dumps(payload)),
        )


initialize_database()


def load_persisted_recovery():
    with sqlite3.connect(DB_FILE) as connection:
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


def validate_config_update(data):
    """Return an error message when a dashboard setting is unsafe."""
    limits = {
        "trade_size": lambda value: value > 0,
        "fee": lambda value: 0 < value < 0.05,
        "min_profit": lambda value: value > 0,
        "max_slippage": lambda value: 0 < value <= 5,
        "interval": lambda value: value > 0,
        "gap_chance": lambda value: 0 <= value <= 1,
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
    return None


def get_readiness():
    """Return safe, non-secret startup status for the dashboard."""
    if state["config"]["execution_mode"] != "real":
        return {"ready": False, "message": "Paper execution is selected."}
    if not state["config"]["real_trading_enabled"]:
        return {"ready": False, "message": "Real trading is not explicitly enabled."}
    validation = bot.validate_real_trading_config()
    return {"ready": validation["ok"], "message": validation["message"]}


def refresh_live_balances():
    """Refresh selected account balances without exposing API credentials."""
    if not real_engine:
        return
    currencies = {"USDT"}
    for symbol in active_symbols:
        currencies.add(bot.base_coin(symbol))
    refreshed = {}
    valuation = {"free_usdt": 0.0, "used_usdt": 0.0, "total_usdt": 0.0}
    for exchange in state["active_exchanges"]:
        try:
            balances = real_engine.fetch_balances(
                exchange, sorted(currencies))
            refreshed[exchange] = balances
            values = real_engine.value_balances_usdt(exchange, balances)
            for key in valuation:
                valuation[key] += values[key]
        except Exception as exc:
            refreshed[exchange] = {"error": str(exc)}
    state["balances"] = refreshed
    state["balance_valuation"] = valuation
    state["portfolio_value"] = valuation["total_usdt"]
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
        wallet = bot.PaperWallet(exchanges, active_symbols,
                                  bot.START_CASH_PER_EXCHANGE, start_prices)
        if execution_mode == "real":
            refresh_live_balances()
            state["start_value"] = None
            state["portfolio_value"] = None
            state["portfolio_source"] = "exchange_balances"
        else:
            state["start_value"] = wallet.total_value(start_prices)
            state["portfolio_value"] = state["start_value"]
            state["portfolio_source"] = "paper_wallet"
        state["error"] = None
    except Exception as e:
        state["error"] = f"engine init failed: {e}"
        raise


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
    bot.init_csv()  # fresh trades.csv, same file the CLI script writes


def scan_loop():
    while not _stop_flag.is_set() and not _emergency_stop.is_set():
        with state_lock:
            cfg = dict(state["config"])
            exchanges = list(state["active_exchanges"])
            symbols = list(active_symbols)

        interval = cfg["interval"]

        try:
            quotes = feed.get_quotes()
        except Exception as e:
            with state_lock:
                state["error"] = f"feed error: {e}"
            time.sleep(interval)
            continue

        now = datetime.now()
        mid_prices = {}
        for sym in symbols:
            if sym in quotes and quotes[sym]:
                mids = [(q["bid"] + q["ask"]) / 2 for q in quotes[sym].values()]
                mid_prices[sym] = sum(mids) / len(mids)

        found = []
        with state_lock:
            state["scan_count"] += 1
            scan_fee = cfg["fee"]
            if cfg["execution_mode"] == "real" and real_engine:
                scan_fee = real_engine.taker_fee(exchanges[0])
            if cfg["strategy"] == "triangular":
                cycle = bot.find_best_triangular_opportunity(
                    quotes, exchanges[0], cfg["trade_size"], scan_fee,
                    cfg.get("triangular_routes", []))
                if cycle:
                    state["chart_label"] = "Triangular cycle return"
                    state["latest_cycle"] = cycle
                    state["chart_series"].append(round(cycle["final_usdt"], 6))
                    state["chart_series"] = state["chart_series"][-60:]
            else:
                state["chart_label"] = "Market price"
            scan_symbols = ["triangular"] if cfg["strategy"] == "triangular" else symbols
            for sym in scan_symbols:
                if cfg["strategy"] != "triangular" and (sym not in quotes or len(quotes[sym]) < 2):
                    continue
                state["attempts_count"] += 1
                opp = (
                    True
                    if cfg["strategy"] == "triangular"
                    else bot.find_opportunity(quotes[sym])
                )
                if not opp:
                    continue
                if cfg["strategy"] == "cross_exchange":
                    buy_ex, sell_ex, ask, bid, net_pct = opp

                try:
                    if cfg["strategy"] == "triangular":
                        opportunity = bot.find_best_triangular_opportunity(
                            quotes, exchanges[0], cfg["trade_size"], scan_fee,
                            cfg.get("triangular_routes", []))
                        if not opportunity:
                            continue
                        buy_ex = sell_ex = exchanges[0]
                        ask = opportunity["prices"][opportunity["symbols"][0]]
                        bid = opportunity["prices"][opportunity["symbols"][2]]
                        net_pct = opportunity["profit_pct"]
                        if cfg["execution_mode"] == "real":
                            result = real_engine.execute_triangular(
                                exchanges[0], cfg["trade_size"], opportunity["prices"],
                                opportunity["symbols"])
                            usdt = cfg["trade_size"] + result["profit_usdt"]
                        else:
                            profit = opportunity["profit_usdt"]
                            usdt = cfg["trade_size"] + profit
                            result = {"buy_order": {}, "sell_order": {}}
                    elif cfg["execution_mode"] == "real":
                        result = real_engine.execute_arbitrage(
                            buy_ex, sell_ex, sym, cfg["trade_size"], ask, bid)
                        usdt = cfg["trade_size"] + result["profit_usdt"]
                    else:
                        result = wallet.execute_arbitrage(
                            buy_ex, sell_ex, sym, cfg["trade_size"], ask, bid, cfg["fee"])
                        if result is None:
                            continue
                        _, usdt = result
                except bot.UnhedgedPositionError as exc:
                    recovery = {
                        "time": now.isoformat(timespec="seconds"),
                        "status": "manual_recovery_required",
                        "symbol": exc.symbol,
                        "buy_exchange": exc.buy_exchange,
                        "sell_exchange": exc.sell_exchange,
                        "recovery_exchange": exc.recovery_exchange,
                        "quantity": exc.quantity,
                        "buy_order_id": exc.buy_order.get("id"),
                        "error": str(exc.cause),
                    }
                    state["unhedged_positions"].insert(0, recovery)
                    state["unhedged_positions"] = state["unhedged_positions"][:20]
                    persist_recovery(recovery)
                    state["error"] = str(exc)
                    state["running"] = False
                    _stop_flag.set()
                    break
                except Exception as exc:
                    state["error"] = f"Execution halted: {exc}"
                    state["running"] = False
                    _stop_flag.set()
                    break

                profit = usdt - cfg["trade_size"]
                state["total_profit"] += profit
                state["trades_count"] += 1

                trade = {
                    "time": now.isoformat(timespec="seconds"),
                    "symbol": sym,
                    "buy_exchange": buy_ex,
                    "sell_exchange": sell_ex,
                    "buy_price": round(ask, 6),
                    "sell_price": round(bid, 6),
                    "trade_size_usdt": cfg["trade_size"],
                    "profit_usdt": round(profit, 4),
                    "net_profit_pct": round(net_pct, 3),
                    "buy_order_id": (
                        result.get("buy_order", {}).get("id")
                        if cfg["execution_mode"] == "real" else None
                    ),
                    "sell_order_id": (
                        result.get("sell_order", {}).get("id")
                        if cfg["execution_mode"] == "real" else None
                    ),
                    "middle_order_id": (
                        result.get("middle_order", {}).get("id")
                        if cfg["strategy"] == "triangular" and cfg["execution_mode"] == "real"
                        else None
                    ),
                }
                state["trades"].insert(0, trade)
                if len(state["trades"]) > MAX_TRADES:
                    state["trades"].pop()
                state["recent"].insert(0, {"type": "hit", **trade})
                persist_trade(trade)

                bot.log_trade([
                    trade["time"], sym, buy_ex, sell_ex,
                    f"{ask:.6f}", f"{bid:.6f}", cfg["trade_size"],
                    f"{profit:.4f}", f"{net_pct:.3f}",
                    trade["buy_order_id"] or "", trade["sell_order_id"] or "",
                    cfg["execution_mode"], trade["middle_order_id"] or "",
                ])
                found.append(trade)

            if not found:
                state["recent"].insert(0, {
                    "type": "miss",
                    "time": now.isoformat(timespec="seconds"),
                    "scan_num": state["scan_count"],
                })
            if len(state["recent"]) > MAX_RECENT:
                state["recent"] = state["recent"][:MAX_RECENT]

            if mid_prices and cfg["execution_mode"] != "real":
                state["portfolio_value"] = wallet.total_value(mid_prices)
            state["quotes"] = quotes
            state["mid_prices"] = mid_prices
            if cfg["execution_mode"] == "real" and state["scan_count"] % 5 == 0:
                refresh_live_balances()
            state["error"] = None

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
    with sqlite3.connect(DB_FILE) as connection:
        trades = connection.execute(
            "SELECT time, symbol, buy_exchange, sell_exchange, trade_size_usdt, "
            "profit_usdt, net_profit_pct, buy_order_id, middle_order_id, "
            "sell_order_id, status FROM trades ORDER BY id DESC LIMIT 500"
        ).fetchall()
        recovery = connection.execute(
            "SELECT payload FROM recovery_positions ORDER BY created_at DESC"
        ).fetchall()
    trade_fields = (
        "time", "symbol", "buy_exchange", "sell_exchange", "trade_size_usdt",
        "profit_usdt", "net_profit_pct", "buy_order_id", "middle_order_id",
        "sell_order_id", "status",
    )
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
        state["error"] = None
    return jsonify({"ok": True, "close_order_id": close_order.get("id")})


@app.route("/api/start", methods=["POST"])
def api_start():
    global _thread
    with state_lock:
        if _emergency_stop.is_set():
            return jsonify({
                "ok": False,
                "error": "Emergency stop engaged. Reset is required before restarting.",
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
    _stop_flag.set()
    with state_lock:
        state["running"] = False
    return jsonify({"ok": True})


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
    _stop_flag.set()
    with state_lock:
        state["running"] = False
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

    needs_rebuild = False
    with state_lock:
        for key in ("trade_size", "fee", "min_profit", "max_slippage", "interval", "gap_chance"):
            if key in data:
                state["config"][key] = float(data[key])

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


if __name__ == "__main__":
    import socket

    # Direct server launches must load the same private configuration as
    # start_live.py before building the exchange/feed engine.
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

    # Bind to 127.0.0.1 only (not 0.0.0.0) — this is a local dev tool,
    # and binding to "all interfaces" is what commonly triggers Windows'
    # WinError 10013 ("socket forbidden by access permissions") when a
    # port is reserved (Hyper-V/WSL2 do this a lot) or a firewall rule
    # blocks it. If a port is blocked, try the next one automatically.
    HOST = "127.0.0.1"
    CANDIDATE_PORTS = [5000, 5050, 5055, 8000, 8080]

    chosen_port = None
    for port in CANDIDATE_PORTS:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((HOST, port))
            s.close()
            chosen_port = port
            break
        except OSError:
            s.close()
            continue

    if chosen_port is None:
        print("=" * 64)
        print("  Could not bind to any of:", CANDIDATE_PORTS)
        print("  Every candidate port is blocked on this machine.")
        print("  Try running as Administrator, or check:")
        print("    netsh interface ipv4 show excludedportrange protocol=tcp")
        print("  and pick a port outside any listed reserved range.")
        print("=" * 64)
        raise SystemExit(1)

    print("=" * 64)
    print(f"  Arbitrage bot backend running — open http://localhost:{chosen_port}")
    if chosen_port != 5000:
        print(f"  (port 5000 was unavailable, used {chosen_port} instead)")
    print("=" * 64)
    app.run(host=HOST, port=chosen_port, debug=False)