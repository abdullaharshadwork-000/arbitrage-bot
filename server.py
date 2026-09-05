#!/usr/bin/env python3
"""
=====================================================================
 ARBITRAGE BOT — WEB SERVER
=====================================================================
Wraps the classes and functions already defined in arbitrage_bot.py
(PaperWallet, DemoFeed, LiveFeed, find_opportunity, log_trade, ...)
in a small Flask app so the canonical dashboard (dashboard-pro.html)
can start, stop, and configure the bot and read its live state over HTTP,
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
import csv
import io
import os
import secrets
import socket
import threading
import time
import math
import json
import logging
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

from flask import Flask, g, jsonify, redirect, request, send_from_directory, Response, session
from werkzeug.security import check_password_hash, generate_password_hash

import arbitrage_bot as bot
from arbicore import alerts, auth as auth_security, config as arbiconfig, reconcile, risk, safety
from arbicore.vault import CredentialVault

app = Flask(__name__, static_folder=None)
# Overridable so a test run - or a second instance - never writes into the
# operator's real trade history.
DB_FILE = Path(os.environ.get("ARBICORE_DB") or Path(__file__).with_name("arbicore.db"))
DEFAULT_ADMIN_USERNAME = os.environ.get("ARBICORE_ADMIN_USERNAME", "admin").strip()
DEFAULT_ADMIN_PASSWORD = os.environ.get("ARBICORE_ADMIN_PASSWORD", "Admin@12345")
DEFAULT_ADMIN_EMAIL = os.environ.get("ARBICORE_ADMIN_EMAIL", "admin@localhost")
credential_vault = CredentialVault()
app.config.update(
    SECRET_KEY=os.environ.get("ARBICORE_SESSION_SECRET") or secrets.token_urlsafe(48),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=os.environ.get("ARBICORE_HTTPS", "0") == "1",
    PERMANENT_SESSION_LIFETIME=3600,
)

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
SERVER_STARTED_AT = time.time()
request_windows = defaultdict(deque)
request_metrics = {"count": 0, "errors": 0, "total_ms": 0.0}
logger = logging.getLogger("arbicore")
if not logger.handlers:
    logging.basicConfig(level=os.environ.get("ARBICORE_LOG_LEVEL", "INFO"),
                        format="%(message)s")


@app.before_request
def begin_request():
    g.request_started = time.perf_counter()


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
    now = time.monotonic()
    key = (request.remote_addr or "unknown", request.path)
    window = request_windows[key]
    while window and now - window[0] > 60:
        window.popleft()
    limit = 5 if request.path in {"/api/auth/login", "/api/auth/password-reset/request"} else 60
    if len(window) >= limit:
        return jsonify({"ok": False, "error": "Too many requests; retry in one minute."}), 429
    window.append(now)
    if not origin_is_this_server(request.headers.get("Origin")):
        dashboard_url = request.host_url.rstrip("/")
        return jsonify({
            "ok": False,
            "error": ("Refused: this request came from another page. Open the "
                      f"dashboard at {dashboard_url} and use it there."),
        }), 403
    token = presented_token()
    if not token or not secrets.compare_digest(token, API_TOKEN):
        dashboard_url = request.host_url.rstrip("/")
        return jsonify({
            "ok": False,
            "error": ("Refused: no valid session token. Load the dashboard from "
                      f"this server ({dashboard_url}) rather than opening "
                      f"the HTML file directly, or send the {TOKEN_HEADER} "
                      "header printed at startup."),
        }), 403
    if (request.path.startswith("/api/")
            and not request.path.startswith("/api/auth/")
            and request_user() is None):
        return jsonify({"ok": False, "error": "Authentication required"}), 401
    return None


@app.after_request
def issue_token_cookie(response):
    """Hand the token to anything this server serves.

    Done for every successfully served page rather than only for "/" so future
    aliases cannot accidentally render a dashboard whose buttons all fail.

    Only on reads, and only on ones that succeeded: a rejected POST must not
    answer with the very credential it was rejected for lacking.
    """
    if (request.method in READ_ONLY_METHODS
            and response.status_code < 400
            and request.cookies.get(TOKEN_COOKIE) != API_TOKEN):
        response.set_cookie(TOKEN_COOKIE, API_TOKEN, samesite="Strict",
                            httponly=True, path="/")
    elapsed_ms = (time.perf_counter() - getattr(g, "request_started", time.perf_counter())) * 1000
    request_metrics["count"] += 1
    request_metrics["errors"] += int(response.status_code >= 400)
    request_metrics["total_ms"] += elapsed_ms
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "font-src 'self' data:; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
    )
    logger.info(json.dumps({"event": "http_request", "method": request.method,
                            "path": request.path, "status": response.status_code,
                            "duration_ms": round(elapsed_ms, 1)}, separators=(",", ":")))
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
    "paper_portfolio_value": 0.0,
    "live_portfolio_value": None,
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
    "connection_verified_at": None,
    "connection_verification": None,
    "config": {
        "mode": bot.MODE,               # "demo" | "live"
        "execution_mode": bot.EXECUTION_MODE,  # "paper" | "real"
        "strategy": bot.TRADING_STRATEGY,
        "real_trading_enabled": bot.REAL_TRADING_ENABLED,
        "sandbox_mode": bot.SANDBOX_MODE,
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
        "max_trades_per_hour": int(os.environ.get("ARBICORE_MAX_TRADES_PER_HOUR", "12")),
    },
    "active_exchanges": list(bot.EXCHANGES),
    "active_symbols": list(bot.SYMBOLS),
    "risk": {},
    "alerts": {},
    "startup_check": None,
    "feed_health": {},
    "execution_safety": {},
    "error": None,
    "owner_user_id": None,
}

# Names and roles seen during this local server process. No passwords or API
# credentials are stored here, and the directory resets on restart.
session_users = {}
user_credentials = {}


def request_user():
    """Resolve the signed browser session to a fresh database user record."""
    user_id = session.get("user_id")
    if user_id is not None:
        with db() as connection:
            session_id = session.get("session_id")
            if session_id:
                active = connection.execute(
                    "SELECT 1 FROM auth_sessions WHERE session_id = ? AND user_id = ? "
                    "AND revoked_at IS NULL AND expires_at > ?",
                    (session_id, int(user_id), time.time()),
                ).fetchone()
                if not active:
                    session.clear()
                    return None
            row = connection.execute(
                "SELECT id, username, email, role, created_at FROM users WHERE id = ?",
                (int(user_id),),
            ).fetchone()
        if row:
            return dict(zip(("id", "username", "email", "role", "created_at"), row))
        session.clear()
    # Headless operational scripts authenticate with the explicit token header.
    # Browser cookies never enter this path, so this cannot collapse two browser
    # users back into shared identity.
    header_token = request.headers.get(TOKEN_HEADER)
    if header_token and secrets.compare_digest(header_token, API_TOKEN):
        return {"id": 0, "username": "service", "email": "", "role": "admin",
                "created_at": ""}
    # Existing unit tests inject a user directly. Never accept this fallback in
    # a running server.
    if app.testing:
        return state.get("current_user")
    return None


def user_identity(user):
    return user.get("id") or f"test:{user.get('username', 'anonymous')}"


def require_engine_owner(user, claim=False):
    """Return an error when another account owns the single live worker."""
    if not user:
        return "Authentication required"
    identity = user_identity(user)
    owner_id = state.get("owner_user_id")
    # Ownership protects an active worker, not an abandoned stopped session.
    # This also prevents a crashed/closed browser from locking the terminal
    # until the server is restarted.
    if claim and owner_id not in (None, identity) and not state.get("running"):
        state["owner_user_id"] = None
        owner_id = None
    if owner_id is None and claim:
        state["owner_user_id"] = identity
        activate_user_context(identity)
        for exchange, credentials in user_credentials.get(identity, {}).items():
            bot.EXCHANGE_CREDENTIALS[exchange] = dict(credentials)
        return None
    if owner_id not in (None, identity):
        return "Another signed-in account owns the trading engine."
    return None


def load_user_config(user_id):
    """Return a validated saved configuration without mutating global state."""
    if not isinstance(user_id, int):
        return None
    with db() as connection:
        row = connection.execute(
            "SELECT payload FROM user_configs WHERE user_id = ?", (user_id,)
        ).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row[0])
        config = payload.get("config") or {}
        error = validate_config_update(config)
        if error:
            logger.warning("Ignoring invalid saved config for user %s: %s", user_id, error)
            return None
        return payload
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring unreadable saved config for user %s: %s", user_id, exc)
        return None


def activate_user_context(user_id):
    """Load one owner's saved controls while the worker is stopped.

    Caller holds ``state_lock``. Network clients are intentionally discarded,
    not rebuilt here: merely signing in must never contact an exchange or
    submit anything. The next Start/Test action initializes them deliberately.
    """
    global wallet, feed, real_engine, active_symbols
    payload = load_user_config(user_id)
    if payload:
        state["config"].update(payload.get("config") or {})
        exchanges = [item for item in payload.get("exchanges", [])
                     if item in bot.EXCHANGES_MASTER]
        symbols = [item for item in payload.get("symbols", [])
                   if item in bot.SYMBOLS_MASTER]
        if exchanges:
            state["active_exchanges"] = exchanges
        if symbols:
            state["active_symbols"] = symbols
    wallet = None
    feed = None
    real_engine = None
    active_symbols = []
    state["connection_verified_at"] = None
    state["connection_verification"] = None
    state["exchange_status"] = []
    state["balances"] = {}
    state["balance_valuation"] = {"free_usdt": 0.0, "used_usdt": 0.0,
                                  "total_usdt": 0.0}


def owner_read_error(user):
    if not user:
        return "Authentication required"
    if user.get("role") == "admin":
        return None
    return require_engine_owner(user)


@contextlib.contextmanager
def db():
    """A committed and then closed connection.

    `with sqlite3.connect(...)` commits the transaction but does not close the
    connection, so every helper below leaked a handle - visible as a
    ResourceWarning under the test suite, and as a slowly climbing descriptor
    count in a server that is meant to run for weeks.
    """
    connection = sqlite3.connect(DB_FILE, timeout=10.0)
    connection.execute("PRAGMA foreign_keys = ON")
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
    "user_id": "INTEGER",
    "expected_profit_usdt": "REAL",
    "realized_slippage_usdt": "REAL",
}
RECOVERY_COLUMNS = {"user_id": "INTEGER"}
BALANCE_COLUMNS = {"user_id": "INTEGER"}
ORDER_INTENT_COLUMNS = {
    "exchange_order_id": "TEXT", "filled_quantity": "TEXT",
    "average_price": "TEXT", "fee_cost": "TEXT", "fee_currency": "TEXT",
    "error": "TEXT", "details": "TEXT",
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
                payload TEXT NOT NULL,
                user_id INTEGER,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                email TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('trader', 'admin')),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS exchange_credentials (
                user_id INTEGER NOT NULL,
                exchange TEXT NOT NULL,
                ciphertext TEXT NOT NULL,
                key_hint TEXT NOT NULL,
                rotated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, exchange),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                user_id INTEGER,
                action TEXT NOT NULL,
                outcome TEXT NOT NULL,
                details TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_configs (
                user_id INTEGER PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS order_intents (
                client_order_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                exchange TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS soak_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                successful_cycles INTEGER NOT NULL DEFAULT 0,
                injected_failures INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id INTEGER PRIMARY KEY,
                experience_mode TEXT NOT NULL DEFAULT 'beginner'
                    CHECK (experience_mode IN ('beginner', 'expert')),
                onboarding_completed INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS user_consents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                consent_type TEXT NOT NULL,
                version TEXT NOT NULL,
                accepted_at TEXT NOT NULL,
                ip_address TEXT,
                UNIQUE (user_id, consent_type, version),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS ledger_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                entry_type TEXT NOT NULL,
                asset TEXT NOT NULL,
                amount REAL NOT NULL,
                reference TEXT NOT NULL,
                details TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS user_security (
                user_id INTEGER PRIMARY KEY,
                totp_secret TEXT,
                totp_enabled INTEGER NOT NULL DEFAULT 0,
                recovery_hashes TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS auth_sessions (
                session_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at REAL NOT NULL,
                revoked_at TEXT,
                user_agent TEXT,
                ip_address TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS auth_login_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempted_at REAL NOT NULL,
                username TEXT NOT NULL,
                ip_address TEXT,
                successful INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at REAL NOT NULL,
                used_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS trades_time ON trades (time);
            CREATE INDEX IF NOT EXISTS audit_events_time ON audit_events (created_at);
            CREATE INDEX IF NOT EXISTS auth_login_attempts_lookup
                ON auth_login_attempts (username, ip_address, attempted_at);
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
        """)
        _add_missing_columns(connection, "trades", TRADE_COLUMNS)
        _add_missing_columns(connection, "recovery_positions", RECOVERY_COLUMNS)
        _add_missing_columns(connection, "balance_snapshots", BALANCE_COLUMNS)
        _add_missing_columns(connection, "order_intents", ORDER_INTENT_COLUMNS)
        connection.execute("CREATE INDEX IF NOT EXISTS balance_snapshots_user_time ON balance_snapshots (user_id, created_at)")
        connection.execute("CREATE INDEX IF NOT EXISTS order_intents_user_time ON order_intents (user_id, created_at)")
        connection.execute("CREATE INDEX IF NOT EXISTS soak_runs_user_time ON soak_runs (user_id, started_at)")
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (1, ?)",
            (datetime.now().isoformat(timespec="seconds"),),
        )
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (2, ?)",
            (datetime.now().isoformat(timespec="seconds"),),
        )
        connection.execute(
            """INSERT OR IGNORE INTO users
               (username, email, password_hash, role, created_at)
               VALUES (?, ?, ?, 'admin', ?)""",
            (DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_EMAIL,
             generate_password_hash(DEFAULT_ADMIN_PASSWORD),
             datetime.now().isoformat(timespec="seconds")),
        )


def persist_trade(trade):
    owner_id = state.get("owner_user_id") if isinstance(state.get("owner_user_id"), int) else None
    with db() as connection:
        cursor = connection.execute(
            """INSERT INTO trades
            (time, symbol, buy_exchange, sell_exchange, trade_size_usdt,
             profit_usdt, net_profit_pct, buy_order_id, middle_order_id,
             sell_order_id, status, buy_price, sell_price, execution_mode,
             strategy, filled_quantity, unsold_dust, user_id,
             expected_profit_usdt, realized_slippage_usdt)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (trade["time"], trade["symbol"], trade["buy_exchange"],
             trade["sell_exchange"], trade["trade_size_usdt"],
             trade["profit_usdt"], trade["net_profit_pct"],
             trade.get("buy_order_id"), trade.get("middle_order_id"),
             trade.get("sell_order_id"), trade.get("status"),
             trade.get("buy_price"), trade.get("sell_price"),
             trade.get("execution_mode"), trade.get("strategy"),
             trade.get("filled_quantity"), trade.get("unsold_dust"),
             owner_id,
             trade.get("expected_profit_usdt"), trade.get("realized_slippage_usdt")),
        )
        if owner_id is not None:
            connection.execute(
                "INSERT INTO ledger_entries "
                "(user_id, created_at, entry_type, asset, amount, reference, details) "
                "VALUES (?, ?, 'realized_pnl', 'USDT', ?, ?, ?)",
                (owner_id, trade["time"], float(trade.get("profit_usdt") or 0),
                 f"trade:{cursor.lastrowid}", json.dumps({
                     "symbol": trade.get("symbol"), "status": trade.get("status"),
                     "execution_mode": trade.get("execution_mode"),
                     "expected_profit_usdt": trade.get("expected_profit_usdt"),
                     "realized_slippage_usdt": trade.get("realized_slippage_usdt"),
                 }, sort_keys=True)),
            )


def persist_recovery(position):
    with db() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO recovery_positions "
            "(buy_order_id, created_at, payload, user_id) VALUES (?, ?, ?, ?)",
            (position.get("buy_order_id") or position["time"],
             position["time"], json.dumps(position),
             state.get("owner_user_id") if isinstance(state.get("owner_user_id"), int) else None),
        )


def remove_persisted_recovery(order_id):
    with db() as connection:
        connection.execute("DELETE FROM recovery_positions WHERE buy_order_id = ?", (order_id,))


def persist_balances(balances, valuation):
    payload = {"balances": balances, "valuation": valuation}
    owner = state.get("owner_user_id")
    with db() as connection:
        connection.execute(
            "INSERT INTO balance_snapshots (created_at, payload, user_id) VALUES (?, ?, ?)",
            (datetime.now().isoformat(timespec="seconds"), json.dumps(payload),
             owner if isinstance(owner, int) else None),
        )


def load_expected_balances(user_id):
    """Last known free balances for restart-time drift reconciliation."""
    if not isinstance(user_id, int):
        return None
    with db() as connection:
        row = connection.execute(
            "SELECT payload FROM balance_snapshots WHERE user_id = ? "
            "ORDER BY id DESC LIMIT 1", (user_id,),
        ).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row[0])
        result = {}
        for exchange, currencies in (payload.get("balances") or {}).items():
            if not isinstance(currencies, dict) or "error" in currencies:
                continue
            result[exchange] = {
                currency: (values.get("free", 0.0)
                           if isinstance(values, dict) else values)
                for currency, values in currencies.items()
            }
        return result or None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


initialize_database()


def audit_event(action, outcome="ok", details=None, user=None):
    """Write a secret-free security/operations event."""
    actor = user if user is not None else request_user()
    safe_details = json.dumps(details or {}, sort_keys=True, separators=(",", ":"))
    with db() as connection:
        connection.execute(
            "INSERT INTO audit_events (created_at, user_id, action, outcome, details) "
            "VALUES (?, ?, ?, ?, ?)",
            (datetime.now().isoformat(timespec="seconds"),
             actor.get("id") if actor else None, action, outcome, safe_details),
        )


def persist_encrypted_credentials(user_id, exchange, api_key, api_secret):
    if not credential_vault.enabled:
        return False
    ciphertext = credential_vault.encrypt(api_key, api_secret)
    with db() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO exchange_credentials "
            "(user_id, exchange, ciphertext, key_hint, rotated_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, exchange, ciphertext, arbiconfig.Credentials._mask(api_key),
             datetime.now().isoformat(timespec="seconds")),
        )
    return True


def load_encrypted_credentials(user_id):
    if not credential_vault.enabled:
        return {}
    with db() as connection:
        rows = connection.execute(
            "SELECT exchange, ciphertext FROM exchange_credentials WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    return {exchange: credential_vault.decrypt(ciphertext)
            for exchange, ciphertext in rows}


def persist_user_config(user_id):
    with state_lock:
        payload = {
            "config": dict(state["config"]),
            "exchanges": list(state["active_exchanges"]),
            "symbols": list(state["active_symbols"]),
        }
    with db() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO user_configs (user_id, payload, updated_at) VALUES (?, ?, ?)",
            (user_id, json.dumps(payload, sort_keys=True),
             datetime.now().isoformat(timespec="seconds")),
        )


def persist_order_intent(client_order_id, exchange, symbol, side, quantity):
    now = datetime.now().isoformat(timespec="seconds")
    owner = state.get("owner_user_id")
    if not isinstance(owner, int):
        raise RuntimeError("A real order cannot be persisted without a user owner.")
    with db() as connection:
        connection.execute(
            "INSERT INTO order_intents "
            "(client_order_id, user_id, exchange, symbol, side, quantity, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'intent', ?, ?)",
            (client_order_id, owner, exchange, symbol, side, str(quantity), now, now),
        )


def update_order_intent(client_order_id, status, details=None):
    details = details if isinstance(details, dict) else {}
    fee = details.get("fee") if isinstance(details.get("fee"), dict) else {}
    with db() as connection:
        connection.execute(
            "UPDATE order_intents SET status = ?, updated_at = ?, "
            "exchange_order_id = COALESCE(?, exchange_order_id), "
            "filled_quantity = COALESCE(?, filled_quantity), "
            "average_price = COALESCE(?, average_price), "
            "fee_cost = COALESCE(?, fee_cost), fee_currency = COALESCE(?, fee_currency), "
            "error = COALESCE(?, error), details = COALESCE(?, details) "
            "WHERE client_order_id = ?",
            (status, datetime.now().isoformat(timespec="seconds"),
             str(details.get("id") or details.get("order_id")) if (details.get("id") or details.get("order_id")) is not None else None,
             str(details.get("filled", details.get("filled_quantity"))) if details.get("filled", details.get("filled_quantity")) is not None else None,
             str(details.get("average", details.get("average_price"))) if details.get("average", details.get("average_price")) is not None else None,
             str(fee.get("cost", details.get("fee_cost"))) if fee.get("cost", details.get("fee_cost")) is not None else None,
             str(fee.get("currency", details.get("fee_currency"))) if fee.get("currency", details.get("fee_currency")) is not None else None,
             str(details.get("error")) if details.get("error") else None,
             json.dumps(details, default=str) if details else None,
             client_order_id),
        )


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
CONNECTION_VERIFICATION_TTL_SECONDS = 300
REQUIRED_TESTNET_CYCLES = int(os.environ.get("ARBICORE_REQUIRED_TESTNET_CYCLES", "100"))
TESTNET_FAILURE_EVERY = int(os.environ.get("ARBICORE_TESTNET_FAILURE_EVERY", "0"))
active_soak_run_id = None
trade_times_hour = deque()


def production_soak_status(user_id):
    if not isinstance(user_id, int) or REQUIRED_TESTNET_CYCLES <= 0:
        return {"required": REQUIRED_TESTNET_CYCLES, "completed": 0, "ready": app.testing}
    with db() as connection:
        completed = connection.execute(
            "SELECT COALESCE(MAX(successful_cycles), 0) FROM soak_runs "
            "WHERE user_id = ? AND status = 'completed'", (user_id,),
        ).fetchone()[0]
    return {"required": REQUIRED_TESTNET_CYCLES, "completed": int(completed),
            "ready": int(completed) >= REQUIRED_TESTNET_CYCLES}


def increment_soak_cycle():
    if active_soak_run_id is None:
        return
    with db() as connection:
        connection.execute(
            "UPDATE soak_runs SET successful_cycles = successful_cycles + 1 "
            "WHERE id = ?", (active_soak_run_id,),
        )


def record_injected_soak_failure():
    if active_soak_run_id is None:
        return
    with db() as connection:
        connection.execute(
            "UPDATE soak_runs SET injected_failures = injected_failures + 1 WHERE id = ?",
            (active_soak_run_id,),
        )

# Config keys whose change can rebuild the feed, wallet or exchange clients.
# A rebuild is only safe with the scan thread stopped and joined.
REBUILD_KEYS = ("mode", "execution_mode", "strategy", "real_trading_enabled", "sandbox_mode",
                "exchanges", "symbols")

# Risk limits and alerts are process-wide, not per-engine-build: a daily loss
# does not stop counting because the operator switched symbols.
notifier = alerts.Notifier(
    webhook_url=getattr(bot, "ALERT_WEBHOOK", ""),
    min_severity=alerts.WARNING)
risk_manager = risk.RiskManager(arbiconfig.Settings())
execution_safety = safety.ExecutionSafety()


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
        "max_trades_per_hour": lambda value: value >= 1,
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


def recommended_scan_interval(config=None, exchanges=None, symbols=None):
    """Conservative floor that keeps public REST requests below bursty rates."""
    config = config or state["config"]
    exchanges = exchanges or state["active_exchanges"]
    symbols = symbols or state["active_symbols"]
    if os.environ.get("ARBICORE_STREAMING") == "1" and config.get("mode") == "live":
        return 0.5
    request_load = max(1, len(exchanges)) * max(1, len(symbols))
    return round(max(1.0, min(10.0, request_load * 0.12)), 1)


def get_readiness():
    """Return safe, non-secret startup status for the dashboard."""
    configured = []
    for exchange in state["active_exchanges"]:
        credentials = bot.resolve_credentials(exchange)
        configured.append({
            "exchange": exchange,
            "configured": credentials.complete,
            "source": credentials.source,
        })
    target = "sandbox" if state["config"].get("sandbox_mode") else "production"
    if state["config"]["execution_mode"] != "real":
        return {"ready": False, "message": "Paper execution is selected.",
                "target": target, "credentials": configured}
    if not state["config"]["real_trading_enabled"]:
        return {"ready": False, "message": "Real trading is not explicitly enabled.",
                "target": target, "credentials": configured}
    validation = bot.validate_real_trading_config()
    verification = state.get("connection_verification") or {}
    expected_context = {
        "target": target,
        "exchanges": list(state["active_exchanges"]),
        "strategy": state["config"].get("strategy"),
    }
    verified_at = verification.get("verified_at")
    verified = False
    if verified_at and all(verification.get(key) == value
                           for key, value in expected_context.items()):
        try:
            age = (datetime.now() - datetime.fromisoformat(verified_at)).total_seconds()
            verified = 0 <= age <= CONNECTION_VERIFICATION_TTL_SECONDS
        except (TypeError, ValueError):
            verified = False
    soak = production_soak_status(state.get("owner_user_id"))
    soak_ready = target != "production" or soak["ready"]
    ready = validation["ok"] and verified and soak_ready
    message = validation["message"]
    if validation["ok"] and target == "production" and not soak_ready:
        message = (f"Complete {soak['required']} successful Binance Testnet cycles first "
                   f"({soak['completed']} recorded).")
    elif validation["ok"] and not verified:
        failed_status = next((item for item in state.get("exchange_status", [])
                              if not item.get("ok") and item.get("error")), None)
        message = (f"Exchange test failed at {failed_status.get('stage', 'connection')}: "
                   f"{failed_status['error']}" if failed_status else
                   "Configuration is valid; test authenticated exchange access before starting.")
    return {"ready": ready, "config_valid": validation["ok"], "message": message,
            "target": target, "credentials": configured,
            "connection_verified_at": verified_at if verified else None,
            "testnet_soak": soak,
            "connection_verification_ttl_seconds": CONNECTION_VERIFICATION_TTL_SECONDS}


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
    state["live_portfolio_value"] = valuation["total_usdt"]


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

    previous_stream = getattr(feed, "stream", None) if feed is not None else None
    if previous_stream:
        previous_stream.stop()

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
    bot.SANDBOX_MODE = bool(state["config"].get("sandbox_mode", bot.SANDBOX_MODE))

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
            expected_balances = load_expected_balances(state.get("owner_user_id"))
            refresh_live_balances()
            state["start_value"] = None
            state["portfolio_value"] = None
            state["portfolio_source"] = "exchange_balances"
            run_startup_check(active_symbols, start_prices, expected_balances)
        else:
            state["startup_check"] = None
            state["start_value"] = wallet.total_value(start_prices)
            state["portfolio_value"] = state["start_value"]
            state["paper_portfolio_value"] = state["start_value"]
            state["portfolio_source"] = "paper_wallet"
        state["error"] = None
    except Exception as e:
        state["error"] = f"engine init failed: {e}"
        raise


def feed_health():
    """What the last quote pass actually managed to price."""
    if feed is None:
        return {}
    health = {
        "symbols": len(getattr(feed, "symbols", []) or []),
        "routes": len(getattr(feed, "routes", []) or []),
        "route_candidates": getattr(feed, "route_candidates", 0),
        "fetch_seconds": round(getattr(feed, "last_fetch_seconds", 0.0), 3),
        "source": getattr(feed, "last_source", "unknown"),
        "stream_error": getattr(feed, "stream_error", ""),
        "stream": (getattr(getattr(feed, "stream", None), "cache", None).health()
                   if getattr(feed, "stream", None) else None),
        "rejected": {name: len(reasons) for name, reasons
                     in (getattr(feed, "rejected_quotes", {}) or {}).items()},
    }
    health["execution_guard"] = execution_safety.snapshot()
    return health


def available_quote_balance(candidate):
    """Free USDT on the venue that funds this candidate, from the last snapshot."""
    exchange = candidate.get("buy_exchange")
    entry = (state.get("balances") or {}).get(exchange)
    if not isinstance(entry, dict) or "error" in entry or "USDT" not in entry:
        return None
    usdt = entry.get("USDT", {}) if isinstance(entry, dict) else {}
    return (float(usdt.get("free", 0.0) or 0.0)
            if isinstance(usdt, dict) else None)


def safe_candidate_size(cfg, candidate):
    """Reduce a real order to the safest current cap; never increase it."""
    configured = float(cfg["trade_size"])
    if cfg.get("execution_mode") != "real":
        return configured
    free_quote = available_quote_balance(candidate)
    # The execution engine performs an authoritative balance preflight.  If a
    # dashboard snapshot is unavailable, do not convert "unknown" to zero;
    # doing that hides recovery/error paths and falsely reports a low balance.
    if free_quote is None:
        return configured
    remaining_loss = max(
        0.0, float(cfg.get("max_daily_loss", 0.0))
        + min(0.0, float(risk_manager.realized_today)))
    sized = execution_safety.dynamic_size(
        configured, free_quote, remaining_loss)
    return float(sized)


def run_startup_check(symbols, prices, expected_balances=None):
    """Refuse to start real trading on top of something left over.

    A resting order, a stranded balance or a skewed clock all mean the account
    is not in the state the bot thinks it is. Starting anyway is how a restart
    turns one unresolved position into two. Blocking findings halt the risk
    manager, which requires an operator to clear them.
    """
    if not real_engine:
        state["startup_check"] = None
        return None

    report = reconcile.startup_check(
        real_engine.clients, symbols,
        expected_balances=expected_balances,
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
        state["connection_verified_at"] = None
        state["connection_verification"] = None
        state["exchange_status"] = []
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
    expected_profit = float(cfg["trade_size"]) * float(candidate["net_pct"]) / 100
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
        "expected_profit_usdt": round(expected_profit, 6),
        "realized_slippage_usdt": round(float(profit) - expected_profit, 6),
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

        if real:
            feed_decision = execution_safety.observe_feed(
                quotes, getattr(quote_feed, "last_fetch_seconds", 0.0))
            with state_lock:
                state["execution_safety"] = execution_safety.snapshot()
            if not feed_decision:
                with state_lock:
                    state["error"] = (f"Execution guard ({feed_decision.limit}): "
                                      f"{feed_decision.reason}")
                    if execution_safety.halted:
                        state["running"] = False
                if execution_safety.halted:
                    notifier.halted(feed_decision.limit, feed_decision.reason,
                                    execution_safety.snapshot())
                    break
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
        if cfg.get("execution_mode") == "real" and cfg.get("sandbox_mode"):
            increment_soak_cycle()
            if TESTNET_FAILURE_EVERY > 0 and scan_num % TESTNET_FAILURE_EVERY == 0:
                record_injected_soak_failure()
                with state_lock:
                    state["error"] = "Injected testnet soak failure; worker recovered."
                time.sleep(interval)
                continue
        with state_lock:
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
            candidate_size = safe_candidate_size(cfg, candidate)
            if candidate_size < float(arbiconfig.DEFAULTS.min_notional_usdt):
                vetoed += 1
                with state_lock:
                    _add_recent({"type": "blocked", "time": now.isoformat(timespec="seconds"),
                                 "symbol": candidate["symbol"], "limit": "dynamic_size",
                                 "reason": (f"safe size {candidate_size:.2f} USDT is below "
                                            "the exchange minimum")})
                continue
            candidate_cfg = dict(cfg)
            candidate_cfg["trade_size"] = candidate_size
            cutoff = time.monotonic() - 3600
            while trade_times_hour and trade_times_hour[0] < cutoff:
                trade_times_hour.popleft()
            if len(trade_times_hour) >= int(cfg.get("max_trades_per_hour", 12)):
                vetoed += 1
                with state_lock:
                    _add_recent({"type": "blocked", "time": now.isoformat(timespec="seconds"),
                                 "symbol": candidate["symbol"], "limit": "hourly_trade_cap",
                                 "reason": "maximum trades per hour reached"})
                continue
            decision = risk_manager.check(candidate_size, candidate["symbol"])
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
                usdt, result = execute_candidate(candidate_cfg, candidate, engine, paper)
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

            profit = usdt - candidate_size
            trade = build_trade_record(candidate_cfg, candidate, result, profit, now)
            risk_manager.record_success(profit)
            edge_decision = execution_safety.record_execution(
                trade["expected_profit_usdt"], profit)
            if not edge_decision:
                halt_error = (f"Execution guard ({edge_decision.limit}): "
                              f"{edge_decision.reason}")
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
            trade_times_hour.append(time.monotonic())
            bot.log_trade([
                trade["time"], trade["symbol"], trade["buy_exchange"],
                trade["sell_exchange"], f"{candidate['ask']:.6f}",
                f"{candidate['bid']:.6f}", candidate_size, f"{profit:.4f}",
                f"{candidate['net_pct']:.3f}", trade["buy_order_id"] or "",
                trade["sell_order_id"] or "", cfg["execution_mode"],
                trade["middle_order_id"] or "",
            ])
            found.append(trade)

            if execution_safety.halted:
                notifier.halted(execution_safety.halt_limit,
                                execution_safety.halt_reason,
                                execution_safety.snapshot())
                break

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
                state["paper_portfolio_value"] = value

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
    """Serve the new professional dashboard"""
    return send_from_directory(".", "dashboard-pro.html")


@app.route("/dashboard.html")
def legacy_dashboard_redirect():
    """Prevent the obsolete mock dashboard from exposing fabricated data."""
    return redirect("/", code=308)


@app.route("/dashboard-pro.html")
def canonical_dashboard_redirect():
    return redirect("/", code=308)


@app.route("/dashboard-pro.js")
def dashboard_javascript():
    return send_from_directory(".", "dashboard-pro.js", mimetype="application/javascript")


@app.route("/dashboard-pro.css")
def dashboard_stylesheet():
    return send_from_directory(".", "dashboard-pro.css", mimetype="text/css")


@app.route("/arbitrage-bot-terminal.html")
def legacy_terminal():
    return send_from_directory(".", "arbitrage-bot-terminal.html")


@app.route("/arbitrage-bot-terminal.js")
def terminal_javascript():
    return send_from_directory(".", "arbitrage-bot-terminal.js", mimetype="application/javascript")


@app.route("/arbitrage-bot-terminal.css")
def terminal_stylesheet():
    return send_from_directory(".", "arbitrage-bot-terminal.css", mimetype="text/css")


@app.route("/legal")
def legal_notice():
    return send_from_directory(".", "LEGAL.md", mimetype="text/plain")


@app.route("/node_modules/@fortawesome/fontawesome-free/<path:filename>")
def fontawesome_asset(filename):
    return send_from_directory("node_modules/@fortawesome/fontawesome-free", filename)


@app.route("/node_modules/chart.js/dist/chart.min.js")
def chartjs_asset():
    return send_from_directory("node_modules/chart.js/dist", "chart.min.js")


@app.route("/favicon.ico")
def favicon():
    """Avoid a noisy browser 404 when a dashboard has no explicit icon."""
    return Response(status=204)


def record_login_attempt(username, successful):
    with db() as connection:
        connection.execute(
            "INSERT INTO auth_login_attempts (attempted_at, username, ip_address, successful) "
            "VALUES (?, ?, ?, ?)",
            (time.time(), str(username or "").casefold(), request.remote_addr,
             1 if successful else 0),
        )


def login_temporarily_locked(username):
    """Limit sustained credential guessing without revealing account existence."""
    cutoff = time.time() - 900
    with db() as connection:
        failures = connection.execute(
            "SELECT COUNT(1) FROM auth_login_attempts WHERE attempted_at >= ? "
            "AND successful = 0 AND (username = ? OR ip_address = ?)",
            (cutoff, str(username or "").casefold(), request.remote_addr),
        ).fetchone()[0]
    return failures >= 10


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    """Authenticate against the persistent local user database."""
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not username or not password:
        return jsonify({"ok": False, "error": "Username and password required"}), 400
    if login_temporarily_locked(username):
        audit_event("auth.login", outcome="locked", details={"username": username.casefold()})
        return jsonify({"ok": False,
                        "error": "Too many failed sign-in attempts. Try again in 15 minutes."}), 429
    with db() as connection:
        row = connection.execute(
            "SELECT id, username, email, password_hash, role, created_at "
            "FROM users WHERE username = ?", (username,)).fetchone()
    if row is None or not check_password_hash(row[3], password):
        record_login_attempt(username, False)
        return jsonify({"ok": False, "error": "Invalid username or password"}), 401

    user = dict(zip(("id", "username", "email", "role", "created_at"),
                    (row[0], row[1], row[2], row[4], row[5])))
    with db() as connection:
        security_row = connection.execute(
            "SELECT totp_secret, totp_enabled, recovery_hashes FROM user_security WHERE user_id = ?",
            (user["id"],),
        ).fetchone()
    if security_row and security_row[1]:
        code = str(data.get("totp_code") or "").strip()
        if not code:
            record_login_attempt(username, False)
            return jsonify({"ok": False, "mfa_required": True,
                            "error": "Authenticator code required"}), 401
        recovery_hash = auth_security.hash_token(code.upper())
        recovery = json.loads(security_row[2] or "[]")
        recovery_match = recovery_hash in recovery
        try:
            secret = credential_vault.decrypt_text(security_row[0])
        except RuntimeError:
            secret = ""
        if not recovery_match and not auth_security.verify_totp(secret, code):
            record_login_attempt(username, False)
            return jsonify({"ok": False, "mfa_required": True,
                            "error": "Invalid authenticator code"}), 401
        if recovery_match:
            recovery.remove(recovery_hash)
            with db() as connection:
                connection.execute(
                    "UPDATE user_security SET recovery_hashes = ?, updated_at = ? WHERE user_id = ?",
                    (json.dumps(recovery), datetime.now().isoformat(timespec="seconds"), user["id"]),
                )
            audit_event("auth.recovery_code_used", user=user)
    session.clear()
    session.permanent = True
    session["user_id"] = user["id"]
    session["session_id"] = secrets.token_urlsafe(18)
    with db() as connection:
        connection.execute(
            "INSERT INTO auth_sessions (session_id, user_id, created_at, expires_at, user_agent, ip_address) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session["session_id"], user["id"], datetime.now().isoformat(timespec="seconds"),
             time.time() + app.permanent_session_lifetime.total_seconds(),
             request.headers.get("User-Agent", "")[:500], request.remote_addr),
        )
    with state_lock:
        session_users[session["session_id"]] = {
            **user, "login_time": datetime.now().isoformat()
        }
        restored = load_encrypted_credentials(user["id"])
        if restored:
            user_credentials[user["id"]] = restored
    audit_event("auth.login", user=user)
    record_login_attempt(username, True)
    return jsonify({
        "ok": True,
        "user": user,
    })


@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    """Create a persistent trader account with a hashed password."""
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    email = data.get("email", "").strip()
    password = data.get("password", "").strip()

    if not all([username, email, password]):
        return jsonify({"ok": False, "error": "All fields required"}), 400
    if len(password) < 12:
        return jsonify({"ok": False, "error": "Password must be at least 12 characters."}), 400
    try:
        with db() as connection:
            cursor = connection.execute(
                """INSERT INTO users
                   (username, email, password_hash, role, created_at)
                   VALUES (?, ?, ?, 'trader', ?)""",
                (username, email, generate_password_hash(password),
                 datetime.now().isoformat(timespec="seconds")),
            )
            user_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        return jsonify({"ok": False, "error": "Username or email already exists"}), 409
    audit_event("auth.register", user={"id": user_id})
    return jsonify({
        "ok": True,
        "message": "User registered successfully",
        "user": {
            "id": user_id,
            "username": username,
            "email": email,
            "role": "trader"
        }
    })


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    """Handle user logout"""
    session_id = session.get("session_id")
    user_id = session.get("user_id")
    with state_lock:
        engine_continues = bool(state.get("running") and
                                state.get("owner_user_id") == user_id)
        if not state.get("running") and state.get("owner_user_id") == user_id:
            state["owner_user_id"] = None
    session.clear()
    if session_id:
        with db() as connection:
            connection.execute(
                "UPDATE auth_sessions SET revoked_at = ? WHERE session_id = ?",
                (datetime.now().isoformat(timespec="seconds"), session_id),
            )
    with state_lock:
        if session_id:
            session_users.pop(session_id, None)
    return jsonify({"ok": True, "engine_continues": engine_continues,
                    "message": ("Signed out; your trading engine is still running."
                                if engine_continues else "Signed out.")})


TERMS_VERSION = "2026-09-03"


@app.route("/api/onboarding", methods=["GET", "POST"])
def onboarding():
    """Persist experience level and mandatory risk/legal acknowledgement."""
    user = request_user()
    if not user:
        return jsonify({"ok": False, "error": "Authentication required"}), 401
    if request.method == "GET":
        with db() as connection:
            row = connection.execute(
                "SELECT experience_mode, onboarding_completed FROM user_preferences WHERE user_id = ?",
                (user["id"],),
            ).fetchone()
            consents = {item[0] for item in connection.execute(
                "SELECT consent_type FROM user_consents WHERE user_id = ? AND version = ?",
                (user["id"], TERMS_VERSION),
            ).fetchall()}
        return jsonify({
            "ok": True,
            "experience_mode": row[0] if row else "beginner",
            "completed": bool(row[1]) if row else False,
            "risk_accepted": "risk_disclosure" in consents,
            "terms_accepted": "terms_and_privacy" in consents,
            "version": TERMS_VERSION,
        })

    data = request.get_json(force=True) or {}
    experience_mode = data.get("experience_mode", "beginner")
    if experience_mode not in ("beginner", "expert"):
        return jsonify({"ok": False, "error": "Choose Beginner or Expert mode."}), 400
    if data.get("risk_accepted") is not True or data.get("terms_accepted") is not True:
        return jsonify({"ok": False, "error": "Risk disclosure and terms must both be accepted."}), 400
    now = datetime.now().isoformat(timespec="seconds")
    with db() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO user_preferences "
            "(user_id, experience_mode, onboarding_completed, updated_at) VALUES (?, ?, 1, ?)",
            (user["id"], experience_mode, now),
        )
        for consent_type in ("risk_disclosure", "terms_and_privacy"):
            connection.execute(
                "INSERT OR IGNORE INTO user_consents "
                "(user_id, consent_type, version, accepted_at, ip_address) VALUES (?, ?, ?, ?, ?)",
                (user["id"], consent_type, TERMS_VERSION, now, request.remote_addr),
            )
    audit_event("onboarding.complete", details={"experience_mode": experience_mode,
                                                "terms_version": TERMS_VERSION}, user=user)
    return jsonify({"ok": True, "experience_mode": experience_mode,
                    "completed": True, "version": TERMS_VERSION})


@app.route("/api/account/password", methods=["POST"])
def change_password():
    user = request_user()
    if not user:
        return jsonify({"ok": False, "error": "Authentication required"}), 401
    data = request.get_json(force=True) or {}
    current_password = str(data.get("current_password") or "")
    new_password = str(data.get("new_password") or "")
    if len(new_password) < 12:
        return jsonify({"ok": False, "error": "New password must be at least 12 characters."}), 400
    with db() as connection:
        row = connection.execute("SELECT password_hash FROM users WHERE id = ?",
                                 (user["id"],)).fetchone()
    valid_current = bool(row and check_password_hash(row[0], current_password))
    if not valid_current:
        audit_event("account.password_change", outcome="rejected", user=user)
        return jsonify({"ok": False, "error": "Current password is incorrect."}), 403
    with db() as connection:
        connection.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                           (generate_password_hash(new_password), user["id"]))
        connection.execute(
            "UPDATE auth_sessions SET revoked_at = ? WHERE user_id = ?",
            (datetime.now().isoformat(timespec="seconds"), user["id"]),
        )
    session.clear()
    audit_event("account.password_change", user=user)
    return jsonify({"ok": True, "message": "Password changed. Sign in again on every device."})


@app.route("/api/account/mfa/setup", methods=["POST"])
def setup_mfa():
    user = request_user()
    if not user:
        return jsonify({"ok": False, "error": "Authentication required"}), 401
    if not credential_vault.enabled:
        return jsonify({"ok": False, "error": "Configure ARBICORE_MASTER_KEY before enabling MFA."}), 503
    secret = auth_security.new_totp_secret()
    with db() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO user_security "
            "(user_id, totp_secret, totp_enabled, recovery_hashes, updated_at) "
            "VALUES (?, ?, 0, '[]', ?)",
            (user["id"], credential_vault.encrypt_text(secret),
             datetime.now().isoformat(timespec="seconds")),
        )
    uri = f"otpauth://totp/ArbiCore:{user['username']}?secret={secret}&issuer=ArbiCore"
    return jsonify({"ok": True, "secret": secret, "otpauth_uri": uri})


@app.route("/api/account/mfa/confirm", methods=["POST"])
def confirm_mfa():
    user = request_user()
    data = request.get_json(silent=True) or {}
    if not user:
        return jsonify({"ok": False, "error": "Authentication required"}), 401
    with db() as connection:
        row = connection.execute(
            "SELECT totp_secret FROM user_security WHERE user_id = ?", (user["id"],)
        ).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Start MFA setup first."}), 409
    secret = credential_vault.decrypt_text(row[0])
    if not auth_security.verify_totp(secret, data.get("code")):
        return jsonify({"ok": False, "error": "Invalid authenticator code."}), 400
    codes = auth_security.recovery_codes()
    with db() as connection:
        connection.execute(
            "UPDATE user_security SET totp_enabled = 1, recovery_hashes = ?, updated_at = ? WHERE user_id = ?",
            (json.dumps([auth_security.hash_token(code) for code in codes]),
             datetime.now().isoformat(timespec="seconds"), user["id"]),
        )
    audit_event("account.mfa_enabled", user=user)
    return jsonify({"ok": True, "recovery_codes": codes,
                    "message": "MFA enabled. Store these recovery codes securely."})


@app.route("/api/account/security")
def account_security():
    user = request_user()
    if not user:
        return jsonify({"ok": False, "error": "Authentication required"}), 401
    with db() as connection:
        security_row = connection.execute(
            "SELECT totp_enabled, recovery_hashes FROM user_security WHERE user_id = ?",
            (user["id"],),
        ).fetchone()
        attempts = connection.execute(
            "SELECT attempted_at, ip_address, successful FROM auth_login_attempts "
            "WHERE username = ? ORDER BY id DESC LIMIT 20",
            (str(user["username"]).casefold(),),
        ).fetchall()
    hashes = json.loads(security_row[1] or "[]") if security_row else []
    return jsonify({"ok": True, "mfa_enabled": bool(security_row and security_row[0]),
                    "recovery_codes_remaining": len(hashes),
                    "login_history": [{
                        "time": datetime.fromtimestamp(row[0]).isoformat(timespec="seconds"),
                        "ip_address": row[1], "successful": bool(row[2]),
                    } for row in attempts]})


@app.route("/api/account/mfa/disable", methods=["POST"])
def disable_mfa():
    user = request_user()
    data = request.get_json(silent=True) or {}
    if not user:
        return jsonify({"ok": False, "error": "Authentication required"}), 401
    with db() as connection:
        password_row = connection.execute(
            "SELECT password_hash FROM users WHERE id = ?", (user["id"],)
        ).fetchone()
        security_row = connection.execute(
            "SELECT totp_secret, totp_enabled, recovery_hashes FROM user_security WHERE user_id = ?",
            (user["id"],),
        ).fetchone()
    if not password_row or not check_password_hash(password_row[0], str(data.get("password") or "")):
        return jsonify({"ok": False, "error": "Current password is incorrect."}), 403
    if not security_row or not security_row[1]:
        return jsonify({"ok": True, "message": "MFA is already disabled."})
    code = str(data.get("code") or "").strip()
    recovery = json.loads(security_row[2] or "[]")
    recovery_match = auth_security.hash_token(code.upper()) in recovery
    try:
        secret = credential_vault.decrypt_text(security_row[0])
    except RuntimeError:
        secret = ""
    if not recovery_match and not auth_security.verify_totp(secret, code):
        return jsonify({"ok": False, "error": "A valid authenticator or recovery code is required."}), 400
    with db() as connection:
        connection.execute(
            "UPDATE user_security SET totp_secret = NULL, totp_enabled = 0, "
            "recovery_hashes = '[]', updated_at = ? WHERE user_id = ?",
            (datetime.now().isoformat(timespec="seconds"), user["id"]),
        )
        connection.execute(
            "UPDATE auth_sessions SET revoked_at = ? WHERE user_id = ? AND session_id != ? "
            "AND revoked_at IS NULL",
            (datetime.now().isoformat(timespec="seconds"), user["id"], session.get("session_id")),
        )
    audit_event("account.mfa_disabled", user=user)
    return jsonify({"ok": True, "message": "MFA disabled; other devices were signed out."})


@app.route("/api/account/sessions")
def account_sessions():
    user = request_user()
    if not user:
        return jsonify({"ok": False, "error": "Authentication required"}), 401
    with db() as connection:
        rows = connection.execute(
            "SELECT session_id, created_at, expires_at, user_agent, ip_address FROM auth_sessions "
            "WHERE user_id = ? AND revoked_at IS NULL ORDER BY created_at DESC", (user["id"],)
        ).fetchall()
    return jsonify({"ok": True, "sessions": [{
        "id": row[0][-6:], "created_at": row[1], "expires_at": row[2],
        "user_agent": row[3], "ip_address": row[4],
        "current": row[0] == session.get("session_id"),
    } for row in rows]})


@app.route("/api/account/sessions/revoke-others", methods=["POST"])
def revoke_other_sessions():
    user = request_user()
    if not user:
        return jsonify({"ok": False, "error": "Authentication required"}), 401
    current = session.get("session_id")
    now = datetime.now().isoformat(timespec="seconds")
    with db() as connection:
        cursor = connection.execute(
            "UPDATE auth_sessions SET revoked_at = ? WHERE user_id = ? AND session_id != ? AND revoked_at IS NULL",
            (now, user["id"], current),
        )
    audit_event("account.sessions_revoked", details={"count": cursor.rowcount}, user=user)
    return jsonify({"ok": True, "revoked": cursor.rowcount})


@app.route("/api/admin/users", methods=["GET"])
def admin_users():
    """Get all users (admin only)"""
    user = request_user()
    if not user or user.get("role") != "admin":
        return jsonify({"ok": False, "error": "Administrator access required"}), 403
    with state_lock:
        trades = list(state.get("trades", []))
        wins = sum(1 for trade in trades
                   if float(trade.get("profit_usdt", 0) or 0) > 0)
        win_rate = (wins / len(trades) * 100) if trades else 0.0
        active_usernames = {item["username"] for item in session_users.values()}
        trades_count = state.get("trades_count", 0)
        total_profit = state.get("total_profit", 0.0)
    with db() as connection:
        rows = connection.execute(
            "SELECT id, username, email, role, created_at FROM users ORDER BY id"
        ).fetchall()
    users = [{
            "id": row[0],
            "username": row[1],
            "email": row[2],
            "role": row[3],
            "status": "Active" if row[1] in active_usernames else "Signed out",
            "trades": trades_count,
            "profit": total_profit,
            "win_rate": win_rate,
            "join_date": row[4][:10],
        } for row in rows]
    return jsonify({"ok": True, "users": users})


@app.route("/api/admin/stats", methods=["GET"])
def admin_stats():
    """Get platform-wide statistics (admin only)"""
    user = request_user()
    if not user or user.get("role") != "admin":
        return jsonify({"ok": False, "error": "Administrator access required"}), 403
    with state_lock:
        total_profit = state.get("total_profit", 0.0)
        trades = list(state.get("trades", []))
        wins = sum(1 for trade in trades
                   if float(trade.get("profit_usdt", 0) or 0) > 0)

    with db() as connection:
        total_users = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    stats = {
        "total_users": total_users,
        "total_profit": total_profit,
        "platform_volume": sum(float(trade.get("trade_size_usdt", 0) or 0)
                               for trade in trades),
        "avg_win_rate": (wins / len(trades) * 100) if trades else 0.0,
        "active_traders": len({item["id"] for item in session_users.values()}),
        "top_performers": [],
        "platform_health": {
            "api_uptime": "local",
            "avg_response_time": None,
            "active_sessions": len(session_users),
            "errors_24h": 1 if state.get("error") else 0
        }
    }
    return jsonify({"ok": True, "stats": stats})


@app.route("/api/user/stats", methods=["GET"])
def user_stats():
    """Get user-specific statistics"""
    user = request_user()
    if not user:
        return jsonify({"ok": False, "error": "Not logged in"}), 401
    with db() as connection:
        row = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(profit_usdt), 0), "
            "COALESCE(SUM(CASE WHEN profit_usdt > 0 THEN 1 ELSE 0 END), 0) "
            "FROM trades WHERE user_id = ?", (user["id"],)
        ).fetchone()
    total_trades, total_profit, wins = int(row[0]), float(row[1]), int(row[2])
    with state_lock:
        owns_worker = state.get("owner_user_id") == user_identity(user)
        stats = {
            "username": user.get("username"),
            "total_balance": state.get("portfolio_value", 0.0) if owns_worker else 0.0,
            "total_profit": total_profit,
            "win_rate": (wins / total_trades * 100) if total_trades else 0.0,
            "active_trades": 1 if owns_worker and state.get("running") else 0,
            "total_trades": total_trades,
            "member_since": user.get("created_at", "")[:10]
        }

    return jsonify({"ok": True, "stats": stats})


@app.route("/api/user/api-keys", methods=["GET"])
def get_user_api_keys():
    """Return redacted process-memory credential status; never return a secret."""
    user = request_user()
    if not user:
        return jsonify({"ok": False, "error": "Not logged in"}), 401
    with state_lock:
        owner_error = require_engine_owner(user)
        stored = user_credentials.get(user_identity(user), {})
        connected_exchanges = []
        for exchange in ("binance",):
            supplied = stored.get(exchange, {})
            configured = bool(supplied.get("apiKey") and supplied.get("secret"))
            connected_exchanges.append({
                "exchange": exchange,
                "configured": configured,
                "source": "process_memory" if configured else "unset",
                "api_key": arbiconfig.Credentials._mask(supplied.get("apiKey", "")),
                "available": owner_error is None,
            })
    return jsonify({"ok": True, "exchanges": connected_exchanges})


@app.route("/api/user/api-keys", methods=["POST"])
def save_user_api_keys():
    """Install credentials in process memory only, for a local admin session."""
    data = request.get_json() or {}
    user = request_user()
    if not user:
        return jsonify({"ok": False, "error": "Not logged in"}), 401
    with state_lock:
        owner_error = require_engine_owner(user, claim=True)
        if owner_error:
            return jsonify({"ok": False, "error": owner_error}), 409
        if state.get("running"):
            return jsonify({"ok": False,
                            "error": "Pause the engine before changing exchange credentials."}), 409

    exchange = str(data.get("exchange") or "").strip().lower()
    api_key = str(data.get("api_key") or "").strip()
    api_secret = str(data.get("api_secret") or "").strip()
    if exchange != "binance":
        return jsonify({"ok": False, "error": "Only Binance setup is available here."}), 400
    if not api_key or not api_secret:
        return jsonify({"ok": False, "error": "Both Binance API key and secret are required."}), 400
    if len(api_key) > 512 or len(api_secret) > 512:
        return jsonify({"ok": False, "error": "Credential value is unexpectedly long."}), 400
    env_credential = arbiconfig.Credentials.from_env(exchange)
    if env_credential.complete:
        return jsonify({"ok": False,
                        "error": "Server environment credentials already control Binance. Remove them and restart before using the form."}), 409

    with state_lock:
        user_credentials.setdefault(user_identity(user), {})[exchange] = {
            "apiKey": api_key,
            "secret": api_secret,
        }
        bot.EXCHANGE_CREDENTIALS[exchange] = {
            "apiKey": api_key,
            "secret": api_secret,
        }
        state["connection_verified_at"] = None
        state["connection_verification"] = None
        state["exchange_status"] = []
        state.setdefault("user_exchanges", {})[exchange] = {
            "exchange": exchange,
            "configured": True,
            "source": "process_memory",
            "api_key": arbiconfig.Credentials._mask(api_key),
        }
    persistent = persist_encrypted_credentials(
        user["id"], exchange, api_key, api_secret) if user.get("id") else False
    audit_event("credentials.rotate", details={"exchange": exchange,
                                               "persistent": persistent}, user=user)
    return jsonify({
        "ok": True,
        "message": ("Binance credentials encrypted and saved." if persistent else
                    "Binance credentials loaded into memory; configure ARBICORE_MASTER_KEY for encrypted persistence."),
        "exchange": exchange,
        "api_key": arbiconfig.Credentials._mask(api_key),
        "persistent": persistent,
    })


@app.route("/api/auth/password-reset/request", methods=["POST"])
def request_password_reset():
    """Create a short-lived reset token and deliver it through a private webhook."""
    data = request.get_json(silent=True) or {}
    identity = str(data.get("identity") or "").strip()
    generic = {"ok": True, "message":
               "If that account exists, password-reset instructions have been sent."}
    if not identity:
        return jsonify(generic)
    with db() as connection:
        row = connection.execute(
            "SELECT id, username, email FROM users WHERE username = ? OR email = ?",
            (identity, identity),
        ).fetchone()
    webhook = os.environ.get("ARBICORE_PASSWORD_RESET_WEBHOOK", "").strip()
    scheme = urllib.parse.urlparse(webhook).scheme.lower() if webhook else ""
    if scheme not in {"http", "https"} or (os.environ.get("ARBICORE_PRODUCTION") == "1"
                                             and scheme != "https"):
        webhook = ""
    if not row or not webhook:
        audit_event("auth.password_reset_requested",
                    outcome="provider_unavailable" if row and not webhook else "unknown_account",
                    user={"id": row[0]} if row else {})
        return jsonify(generic)
    token = secrets.token_urlsafe(32)
    now = datetime.now().isoformat(timespec="seconds")
    with db() as connection:
        connection.execute(
            "INSERT INTO password_reset_tokens "
            "(token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (auth_security.hash_token(token), row[0], now, time.time() + 900),
        )
    reset_url = request.host_url.rstrip("/") + "/?reset_token=" + urllib.parse.quote(token)
    payload = json.dumps({"event": "password_reset", "username": row[1],
                          "recipient": row[2], "token": token, "reset_url": reset_url,
                          "expires_minutes": 15}).encode("utf-8")
    delivered = False
    try:
        reset_request = urllib.request.Request(
            webhook, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(reset_request, timeout=5) as response:  # nosec B310 - scheme allowlisted above
            delivered = 200 <= response.status < 300
    except (OSError, urllib.error.URLError, urllib.error.HTTPError):
        delivered = False
    audit_event("auth.password_reset_requested",
                outcome="delivered" if delivered else "delivery_failed", user={"id": row[0]})
    return jsonify(generic)


@app.route("/api/auth/password-reset/confirm", methods=["POST"])
def confirm_password_reset():
    data = request.get_json(silent=True) or {}
    token_hash = auth_security.hash_token(str(data.get("token") or ""))
    password = str(data.get("new_password") or "")
    if len(password) < 12:
        return jsonify({"ok": False, "error": "New password must be at least 12 characters."}), 400
    with db() as connection:
        row = connection.execute(
            "SELECT user_id FROM password_reset_tokens WHERE token_hash = ? "
            "AND used_at IS NULL AND expires_at >= ?", (token_hash, time.time()),
        ).fetchone()
        if not row:
            return jsonify({"ok": False, "error": "The reset token is invalid or expired."}), 400
        used_at = datetime.now().isoformat(timespec="seconds")
        connection.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                           (generate_password_hash(password), row[0]))
        connection.execute("UPDATE password_reset_tokens SET used_at = ? WHERE token_hash = ?",
                           (used_at, token_hash))
        connection.execute("UPDATE auth_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                           (used_at, row[0]))
    audit_event("auth.password_reset_completed", user={"id": row[0]})
    return jsonify({"ok": True, "message": "Password reset. Sign in with the new password."})


@app.route("/api/user/api-keys/<exchange>", methods=["DELETE"])
def delete_user_api_key(exchange):
    """Disconnect process-memory credentials; environment keys cannot be deleted here."""
    exchange = exchange.lower()

    user = request_user()
    if not user:
        return jsonify({"ok": False, "error": "Not logged in"}), 401
    with state_lock:
        owner_error = require_engine_owner(user)
        if owner_error:
            return jsonify({"ok": False, "error": owner_error}), 409
        if state.get("running"):
            return jsonify({"ok": False,
                            "error": "Pause the engine before changing exchange credentials."}), 409
        if arbiconfig.Credentials.from_env(exchange).complete:
            return jsonify({"ok": False,
                            "error": "Environment credentials must be removed from the server environment."}), 409
        bot.EXCHANGE_CREDENTIALS[exchange] = {"apiKey": "", "secret": ""}
        user_credentials.get(user_identity(user), {}).pop(exchange, None)
        if not user_credentials.get(user_identity(user)):
            state["owner_user_id"] = None
        if "user_exchanges" in state and exchange in state["user_exchanges"]:
            del state["user_exchanges"][exchange]
        state["connection_verified_at"] = None
        state["connection_verification"] = None
        state["exchange_status"] = []
    if user.get("id"):
        with db() as connection:
            connection.execute(
                "DELETE FROM exchange_credentials WHERE user_id = ? AND exchange = ?",
                (user["id"], exchange),
            )
    audit_event("credentials.delete", details={"exchange": exchange}, user=user)
    return jsonify({
        "ok": True,
        "message": f"{exchange.upper()} process-memory credentials erased."
    })


@app.route("/api/state")
def api_state():
    user = request_user()
    if not user:
        return jsonify({"current_user": None, "running": False})
    with state_lock:
        payload = dict(state)
        payload["current_user"] = user
        identity = user_identity(user)
        owner = state.get("owner_user_id")
        if user.get("role") != "admin" and owner not in (None, identity):
            payload.update({
                "running": False,
                "trades": [],
                "recent": [],
                "balances": {},
                "balance_valuation": {"free_usdt": 0.0, "used_usdt": 0.0,
                                      "total_usdt": 0.0},
                "exchange_status": [],
                "connection_verified_at": None,
                "connection_verification": None,
                "unhedged_positions": [],
                "total_profit": 0.0,
                "portfolio_value": 0.0,
                "live_portfolio_value": None,
                "access_message": "Another account owns the active trading worker.",
            })
        payload["available_exchanges"] = ["binance", "kucoin", "okx", "bybit"]
        payload["available_symbols"] = list(bot.DEMO_START_PRICES)
        payload["recommended_scan_interval"] = recommended_scan_interval()
        if wallet is not None and state["config"]["execution_mode"] == "paper":
            paper_balances = {}
            for exchange, symbols in wallet.usdt.items():
                paper_balances[exchange] = {}
                for symbol, cash in symbols.items():
                    asset = bot.base_coin(symbol)
                    paper_balances[exchange][symbol] = {
                        "asset": asset,
                        "cash_usdt": cash,
                        "coin": wallet.coin[exchange][symbol],
                        "source": "paper",
                    }
            payload["balances"] = paper_balances
        payload["readiness"] = get_readiness()
        return jsonify(payload)


@app.route("/api/readiness")
def api_readiness():
    with state_lock:
        return jsonify(get_readiness())


@app.route("/api/health")
def api_health():
    count = request_metrics["count"]
    with state_lock:
        payload = {
            "ok": state.get("error") is None,
            "uptime_seconds": round(time.time() - SERVER_STARTED_AT, 1),
            "requests": count,
            "errors": request_metrics["errors"],
            "average_response_ms": round(request_metrics["total_ms"] / count, 1) if count else 0.0,
            "engine_running": bool(state.get("running")),
            "worker_alive": bool(_thread and _thread.is_alive()),
            "feed": dict(state.get("feed_health") or {}),
            "exchange": list(state.get("exchange_status") or []),
            "risk": risk_manager.snapshot(),
            "execution_safety": execution_safety.snapshot(),
        }
    return jsonify(payload), 200 if payload["ok"] else 503


@app.route("/api/admin/audit")
def api_admin_audit():
    user = request_user()
    if not user or user.get("role") != "admin":
        return jsonify({"ok": False, "error": "Administrator access required"}), 403
    with db() as connection:
        rows = connection.execute(
            "SELECT created_at, user_id, action, outcome, details "
            "FROM audit_events ORDER BY id DESC LIMIT 500"
        ).fetchall()
    return jsonify({"ok": True, "events": [
        {"created_at": row[0], "user_id": row[1], "action": row[2],
         "outcome": row[3], "details": json.loads(row[4])}
        for row in rows
    ]})


@app.route("/api/admin/production-readiness")
def api_production_readiness():
    """Machine-readable launch blockers; never equate local tests with approval."""
    user = request_user()
    if not user or user.get("role") != "admin":
        return jsonify({"ok": False, "error": "Administrator access required"}), 403
    checks = [
        {"id": "session_secret", "ready": bool(os.environ.get("ARBICORE_SESSION_SECRET")),
         "message": "Persistent session secret configured"},
        {"id": "master_key", "ready": credential_vault.enabled,
         "message": "Credential and MFA encryption configured"},
        {"id": "admin_password", "ready": DEFAULT_ADMIN_PASSWORD != "Admin@12345",
         "message": "Development administrator password replaced"},
        {"id": "https", "ready": bool(app.config.get("SESSION_COOKIE_SECURE")),
         "message": "HTTPS-only session cookies enabled"},
        {"id": "password_delivery", "ready": bool(os.environ.get("ARBICORE_PASSWORD_RESET_WEBHOOK")),
         "message": "Password-reset delivery provider configured"},
        {"id": "database", "ready": False,
         "message": "Managed PostgreSQL and tested point-in-time restore required"},
        {"id": "workers", "ready": False,
         "message": "Isolated supervised worker per trader required"},
        {"id": "testnet", "ready": production_soak_status(user.get("id"))["ready"],
         "message": "Required authenticated Testnet soak completed"},
        {"id": "legal_operations", "ready": False,
         "message": "External legal review and 24/7 incident ownership required"},
    ]
    return jsonify({"ok": True, "production_ready": all(item["ready"] for item in checks),
                    "checks": checks,
                    "warning": "Passing software checks cannot guarantee profit or eliminate trading risk."})


@app.route("/api/balances")
def api_balances():
    error = owner_read_error(request_user())
    if error:
        return jsonify({"ok": False, "error": error}), 403
    with state_lock:
        return jsonify({
            "balances": state["balances"],
            "valuation": state["balance_valuation"],
        })


@app.route("/api/inventory-plan")
def inventory_plan():
    """Explain whether each venue has enough prefunded inventory for one route."""
    user = request_user()
    error = owner_read_error(user)
    if error:
        return jsonify({"ok": False, "error": error}), 403
    with state_lock:
        size = float(state["config"].get("trade_size") or 0)
        strategy = state["config"].get("strategy")
        balances = dict(state.get("balances") or {})
        exchanges = list(state.get("active_exchanges") or [])
        symbols = list(state.get("active_symbols") or [])
    required_usdt = round(size * 1.02, 8)
    venues = []
    for exchange in exchanges:
        venue = balances.get(exchange) or {}
        usdt = venue.get("USDT") if isinstance(venue, dict) else None
        if isinstance(usdt, dict):
            free = float(usdt.get("free") or 0)
        else:
            free = 0.0
        asset_free = {}
        for symbol in symbols:
            asset = bot.base_coin(symbol)
            item = venue.get(asset) if isinstance(venue, dict) else None
            asset_free[asset] = float(item.get("free") or 0) if isinstance(item, dict) else 0.0
        venues.append({
            "exchange": exchange, "free_usdt": free, "required_usdt": required_usdt,
            "usdt_ready": free >= required_usdt, "asset_free": asset_free,
            "shortfall_usdt": round(max(0.0, required_usdt - free), 8),
        })
    if strategy == "triangular":
        ready = any(item["usdt_ready"] for item in venues)
    else:
        ready = any(
            buy["exchange"] != sell["exchange"] and buy["usdt_ready"]
            and any(quantity > 0 for quantity in sell["asset_free"].values())
            for buy in venues for sell in venues
        )
    return jsonify({"ok": True, "strategy": strategy, "symbols": symbols,
                    "trade_size_usdt": size, "venues": venues,
                    "ready": bool(venues) and ready,
                    "note": "Cross-exchange routes require quote currency on the buy venue and prefunded base inventory on the sell venue."})


@app.route("/api/history")
def api_history():
    # The extra columns are appended, so the existing field order the dashboard
    # unpacks is untouched and rows written before the migration still load.
    user = request_user()
    if not user:
        return jsonify({"trades": [], "recovery": []})
    trade_fields = (
        "time", "symbol", "buy_exchange", "sell_exchange", "trade_size_usdt",
        "profit_usdt", "net_profit_pct", "buy_order_id", "middle_order_id",
        "sell_order_id", "status", "buy_price", "sell_price", "execution_mode",
        "strategy", "filled_quantity", "unsold_dust", "user_id",
        "expected_profit_usdt", "realized_slippage_usdt",
    )
    with db() as connection:
        if user.get("role") == "admin":
            trades = connection.execute(
                f"SELECT {', '.join(trade_fields)} FROM trades "
                "ORDER BY id DESC LIMIT 500"
            ).fetchall()
        else:
            trades = connection.execute(
                f"SELECT {', '.join(trade_fields)} FROM trades "
                "WHERE user_id = ? ORDER BY id DESC LIMIT 500",
                (user["id"],),
            ).fetchall()
        if user.get("role") == "admin":
            recovery = connection.execute(
                "SELECT payload FROM recovery_positions ORDER BY created_at DESC"
            ).fetchall()
        else:
            recovery = connection.execute(
                "SELECT payload FROM recovery_positions WHERE user_id = ? "
                "ORDER BY created_at DESC", (user["id"],)
            ).fetchall()
    return jsonify({
        "trades": [dict(zip(trade_fields, row)) for row in trades],
        "recovery": [json.loads(row[0]) for row in recovery],
    })


@app.route("/api/orders")
def api_orders():
    """Persistent order-intent timeline, scoped to the signed-in user."""
    user = request_user()
    if not user:
        return jsonify({"ok": False, "error": "Authentication required"}), 401
    query = ("SELECT client_order_id, user_id, exchange, symbol, side, quantity, "
             "status, created_at, updated_at, exchange_order_id, filled_quantity, "
             "average_price, fee_cost, fee_currency, error FROM order_intents")
    params = ()
    if user.get("role") != "admin":
        query += " WHERE user_id = ?"
        params = (user["id"],)
    query += " ORDER BY id DESC LIMIT 500"
    with db() as connection:
        rows = connection.execute(query, params).fetchall()
    fields = ("client_order_id", "user_id", "exchange", "symbol", "side",
              "quantity", "status", "created_at", "updated_at",
              "exchange_order_id", "filled_quantity", "average_price",
              "fee_cost", "fee_currency", "error")
    return jsonify({"ok": True, "orders": [dict(zip(fields, row)) for row in rows]})


@app.route("/api/execution-quality")
def execution_quality():
    """Measured execution quality from persisted trades and order outcomes."""
    user = request_user()
    if not user:
        return jsonify({"ok": False, "error": "Authentication required"}), 401
    where = "" if user.get("role") == "admin" else " WHERE user_id = ?"
    params = () if user.get("role") == "admin" else (user["id"],)
    with db() as connection:
        trade = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(expected_profit_usdt), 0), "
            "COALESCE(SUM(profit_usdt), 0), COALESCE(SUM(realized_slippage_usdt), 0) "
            "FROM trades" + where, params,
        ).fetchone()
        outcomes = connection.execute(
            "SELECT status, COUNT(*) FROM order_intents" + where + " GROUP BY status", params,
        ).fetchall()
        latency = connection.execute(
            "SELECT AVG((julianday(updated_at) - julianday(created_at)) * 86400000.0) "
            "FROM order_intents" + where, params,
        ).fetchone()[0]
    status_counts = {str(row[0] or "unknown"): int(row[1]) for row in outcomes}
    rejected = sum(count for status, count in status_counts.items()
                   if status.lower() in {"rejected", "canceled", "cancelled", "expired", "failed", "ambiguous"})
    return jsonify({"ok": True, "quality": {
        "trades": int(trade[0]), "expected_profit_usdt": float(trade[1]),
        "realized_profit_usdt": float(trade[2]), "realized_slippage_usdt": float(trade[3]),
        "average_order_resolution_ms": round(float(latency or 0), 1),
        "rejected_orders": rejected, "order_statuses": status_counts,
    }})


@app.route("/api/accounting/summary")
def accounting_summary():
    user = request_user()
    if not user:
        return jsonify({"ok": False, "error": "Authentication required"}), 401
    where = "" if user.get("role") == "admin" else " WHERE user_id = ?"
    params = () if user.get("role") == "admin" else (user["id"],)
    with db() as connection:
        totals = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM ledger_entries" + where,
            params,
        ).fetchone()
        rows = connection.execute(
            "SELECT created_at, entry_type, asset, amount, reference, details "
            "FROM ledger_entries" + where + " ORDER BY id DESC LIMIT 200",
            params,
        ).fetchall()
    return jsonify({
        "ok": True,
        "entry_count": totals[0],
        "realized_pnl_usdt": totals[1],
        "entries": [{
            "created_at": row[0], "entry_type": row[1], "asset": row[2],
            "amount": row[3], "reference": row[4], "details": json.loads(row[5]),
        } for row in rows],
    })


@app.route("/api/test-connection", methods=["POST"])
def api_test_connection():
    user = request_user()
    with state_lock:
        owner_error = require_engine_owner(user)
        if owner_error:
            return jsonify({"ok": False, "error": owner_error}), 409
        if state["config"]["execution_mode"] != "real":
            return jsonify({"ok": False, "error": "Select real execution before testing exchange access."}), 409
        validation = bot.validate_real_trading_config()
        if not validation["ok"]:
            return jsonify({"ok": False, "error": validation["message"]}), 400
        exchanges = list(state["active_exchanges"])
        symbols = list(state["active_symbols"])
        config_snapshot = dict(state["config"])

    try:
        engine = bot.RealExecutionEngine(exchanges)
        statuses = [engine.connection_status(exchange) for exchange in exchanges]
    except Exception as exc:
        error = str(exc)
        statuses = [{
            "exchange": exchange,
            "ok": False,
            "stage": "public_market_initialization",
            "error": error,
            "balance_access": False,
            "trade_access": None,
        } for exchange in exchanges]
        with state_lock:
            state["exchange_status"] = statuses
            state["connection_verified_at"] = None
            state["connection_verification"] = None
        return jsonify({"ok": False, "exchanges": statuses, "error": error}), 502

    # A successful authenticated check should immediately populate the
    # dashboard from the connected account, even while the bot is paused.
    # Starting the engine is not required merely to see one's own balance.
    refreshed_balances = None
    refreshed_valuation = None
    if all(item["ok"] for item in statuses):
        refreshed_balances, refreshed_valuation = collect_live_balances(
            engine, exchanges, symbols)

    safety_errors = []
    if all(item["ok"] for item in statuses):
        trade_size = float(config_snapshot["trade_size"])
        if config_snapshot["strategy"] == "triangular":
            exchange = exchanges[0]
            free_usdt = engine.get_balance_usdt(exchange)
            required = trade_size * 1.02
            if free_usdt < required:
                safety_errors.append(
                    f"{exchange} needs at least {required:.2f} free USDT for a "
                    f"{trade_size:.2f} USDT route plus a 2% fee/rounding buffer; "
                    f"only {free_usdt:.2f} is available.")
            if float(config_snapshot["max_position_notional"]) > free_usdt * 0.50:
                safety_errors.append(
                    "Maximum position must be no more than 50% of free USDT for "
                    "a small production account.")
            if float(config_snapshot["max_daily_loss"]) > free_usdt * 0.10:
                safety_errors.append(
                    "Maximum daily loss must be no more than 10% of free USDT.")
    if not config_snapshot.get("sandbox_mode"):
        soak = production_soak_status(state.get("owner_user_id"))
        if not soak["ready"]:
            safety_errors.append(
                f"Complete {soak['required']} successful Binance Testnet cycles "
                f"before production ({soak['completed']} recorded).")
    all_ok = all(item["ok"] for item in statuses) and not safety_errors
    with state_lock:
        if refreshed_balances is not None and refreshed_valuation is not None:
            publish_live_balances(refreshed_balances, refreshed_valuation)
        verified_at = datetime.now().isoformat() if all_ok else None
        state["exchange_status"] = statuses
        state["connection_verified_at"] = verified_at
        state["connection_verification"] = ({
            "verified_at": verified_at,
            "target": "sandbox" if state["config"].get("sandbox_mode") else "production",
            "exchanges": list(state["active_exchanges"]),
            "strategy": state["config"].get("strategy"),
        } if all_ok else None)
    if refreshed_balances is not None and refreshed_valuation is not None:
        persist_balances(refreshed_balances, refreshed_valuation)
    error = "; ".join(safety_errors) if safety_errors else next(
        (item.get("error") for item in statuses if not item.get("ok")),
        "Exchange access check failed.")
    audit_event("exchange.test", outcome="ok" if all_ok else "rejected",
                details={"exchanges": exchanges,
                         "stages": [item.get("stage") for item in statuses],
                         "safety_errors": safety_errors}, user=user)
    return jsonify({"ok": all_ok, "exchanges": statuses,
                    "safety_errors": safety_errors,
                    "error": None if all_ok else error}), 200 if all_ok else 502


@app.route("/api/recovery")
def api_recovery():
    error = owner_read_error(request_user())
    if error:
        return jsonify({"ok": False, "error": error}), 403
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
    error = owner_read_error(request_user())
    if error:
        return jsonify({"ok": False, "error": error}), 403
    with state_lock:
        return jsonify({"risk": risk_manager.snapshot(),
                        "execution_safety": execution_safety.snapshot(),
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
    if ok:
        execution_safety.resume()
    with state_lock:
        if ok:
            state["error"] = None
        state["risk"] = risk_manager.snapshot()
        state["execution_safety"] = execution_safety.snapshot()
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
    global _thread, active_soak_run_id
    user = request_user()
    with state_lock:
        owner_error = require_engine_owner(user, claim=True)
        if owner_error:
            return jsonify({"ok": False, "error": owner_error}), 409
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
        if execution_safety.halted:
            return jsonify({
                "ok": False,
                "error": (f"Execution guard ({execution_safety.halt_limit}) is latched: "
                          f"{execution_safety.halt_reason}. Review the feed/execution "
                          "condition and explicitly resume the risk controls."),
                "execution_safety": execution_safety.snapshot(),
            }), 409
        if state["running"]:
            return jsonify({"ok": True})
        if state["config"]["execution_mode"] == "real":
            readiness = get_readiness()
            if not readiness["ready"]:
                return jsonify({"ok": False, "error": readiness["message"],
                                "readiness": readiness}), 409
            if real_engine is None:
                return jsonify({"ok": False, "error":
                                "Real execution engine is not initialized. Reapply the configuration."}), 409
            startup = state.get("startup_check") or {}
            if startup.get("blocking"):
                return jsonify({"ok": False, "error":
                                "Startup reconciliation found a blocking account condition.",
                                "startup_check": startup}), 409
        if wallet is None:
            init_engine()
        if (state["config"]["execution_mode"] == "real"
                and state["config"].get("sandbox_mode")
                and isinstance(state.get("owner_user_id"), int)):
            with db() as connection:
                cursor = connection.execute(
                    "INSERT INTO soak_runs (user_id, started_at, status) VALUES (?, ?, 'running')",
                    (state["owner_user_id"], datetime.now().isoformat(timespec="seconds")),
                )
                active_soak_run_id = cursor.lastrowid
        state["running"] = True
    _stop_flag.clear()
    _thread = threading.Thread(target=scan_loop, daemon=True)
    _thread.start()
    audit_event("engine.start", details={"execution_mode": state["config"]["execution_mode"],
                                         "sandbox": state["config"].get("sandbox_mode")}, user=user)
    return jsonify({"ok": True})


@app.route("/api/pause", methods=["POST"])
def api_pause():
    """Stop the loop and wait for it to leave.

    Waiting matters: the reply is the operator's signal that no order is in
    flight any more. Reporting "paused" while a real trade is still reconciling
    is how someone closes the terminal on a half-done position.
    """
    global active_soak_run_id
    stopped = stop_scan_thread()
    with state_lock:
        state["running"] = False
        if not stopped:
            state["error"] = ("Pause requested, but a trade is still being "
                              "reconciled. Do not close the process yet.")
    if stopped and active_soak_run_id is not None:
        with db() as connection:
            connection.execute(
                "UPDATE soak_runs SET completed_at = ?, status = 'completed' WHERE id = ?",
                (datetime.now().isoformat(timespec="seconds"), active_soak_run_id),
            )
        active_soak_run_id = None
    audit_event("engine.pause", outcome="ok" if stopped else "pending")
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
    user = request_user()
    with state_lock:
        owner_error = require_engine_owner(user, claim=True)
    if owner_error:
        return jsonify({"ok": False, "error": owner_error}), 409
    acknowledgement = data.pop("real_trading_ack", None)
    if acknowledgement is not None and acknowledgement != arbiconfig.REAL_TRADING_ACK:
        return jsonify({"ok": False,
                        "error": f"Type {arbiconfig.REAL_TRADING_ACK!r} exactly to enable real execution."}), 400
    safe_adjustments = {}
    if "interval" in data:
        try:
            requested_interval = float(data["interval"])
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "interval must be a number."}), 400
        proposed = {**state["config"], "mode": data.get("mode", state["config"].get("mode"))}
        minimum_interval = recommended_scan_interval(
            proposed, data.get("exchanges", state["active_exchanges"]),
            data.get("symbols", state["active_symbols"]),
        )
        if requested_interval < minimum_interval:
            safe_adjustments["interval"] = minimum_interval
            data["interval"] = minimum_interval
    if user and isinstance(user.get("id"), int) and data.get("execution_mode") == "real":
        with db() as connection:
            preference = connection.execute(
                "SELECT experience_mode, onboarding_completed FROM user_preferences WHERE user_id = ?",
                (user["id"],),
            ).fetchone()
        if preference and preference[0] == "beginner" and preference[1]:
            with state_lock:
                free_usdt = float(state.get("balance_valuation", {}).get("free_usdt") or 0)
            if free_usdt > 0:
                requested_trade = float(data.get("trade_size", state["config"]["trade_size"]))
                safe_trade = max(10.0, min(requested_trade, math.floor(free_usdt * 0.25 * 100) / 100))
                safe_position = max(safe_trade, min(
                    float(data.get("max_position_notional", safe_trade)), free_usdt * 0.50))
                safe_loss = min(float(data.get("max_daily_loss", 1.0)), max(0.10, free_usdt * 0.05))
                for key, value in (("trade_size", safe_trade),
                                   ("max_position_notional", safe_position),
                                   ("max_daily_loss", safe_loss)):
                    if key in data and float(data[key]) != value:
                        safe_adjustments[key] = value
                    data[key] = value
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
    rebuild_error = None
    with state_lock:
        if acknowledgement is not None:
            # Deliberately process-local: an acknowledgement entered in the UI
            # is lost on restart and is never written to the database or HTML.
            bot.REAL_TRADING_ACK = acknowledgement
        for key in ("trade_size", "fee", "min_profit", "max_slippage", "interval",
                    "gap_chance", "max_daily_loss", "max_position_notional"):
            if key in data:
                state["config"][key] = float(data[key])
        # Counts, not amounts: a fractional "2.5 failures in a row" would never
        # compare equal to the integer the risk manager counts up.
        for key in ("max_consecutive_failures", "max_orders_per_minute",
                    "max_trades_per_hour"):
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
                # Real execution requires live mode: auto-switch if needed
                if data["execution_mode"] == "real" and state["config"]["mode"] != "live":
                    state["config"]["mode"] = "live"
                    bot.MODE = "live"

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

        if "sandbox_mode" in data:
            requested_sandbox = bool(data["sandbox_mode"])
            if requested_sandbox != state["config"].get("sandbox_mode", False):
                state["config"]["sandbox_mode"] = requested_sandbox
                bot.SANDBOX_MODE = requested_sandbox
                state["connection_verified_at"] = None
                state["connection_verification"] = None
                state["exchange_status"] = []
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

        should_initialize = (needs_rebuild or
                             (state["config"]["execution_mode"] == "real"
                              and real_engine is None))
        if should_initialize and not state["running"]:
            state["connection_verified_at"] = None
            state["connection_verification"] = None
            state["exchange_status"] = []
            try:
                init_engine()
            except Exception as e:
                state["error"] = str(e)
                rebuild_error = str(e)

    if rebuild_error:
        audit_event("engine.config", outcome="rejected",
                    details={"reason": rebuild_error}, user=user)
        return jsonify({"ok": False, "error": rebuild_error,
                        "needs_rebuild": needs_rebuild}), 400
    if user and user.get("id"):
        persist_user_config(user["id"])
    audit_event("engine.config", details={"needs_rebuild": needs_rebuild}, user=user)
    return jsonify({"ok": True, "needs_rebuild": needs_rebuild,
                    "safe_adjustments": safe_adjustments,
                    "error": state.get("error")})


@app.route("/api/trades.csv")
def api_csv():
    """Export only the trades the authenticated account may view."""
    user = request_user()
    if not user:
        return jsonify({"ok": False, "error": "Authentication required"}), 401
    fields = (
        "time", "symbol", "buy_exchange", "sell_exchange", "buy_price",
        "sell_price", "trade_size_usdt", "profit_usdt", "net_profit_pct",
        "status", "execution_mode", "strategy", "expected_profit_usdt",
        "realized_slippage_usdt",
    )
    query = f"SELECT {', '.join(fields)} FROM trades"
    parameters = ()
    if user.get("role") != "admin":
        query += " WHERE user_id = ?"
        parameters = (user["id"],)
    query += " ORDER BY id DESC"
    with db() as connection:
        rows = connection.execute(query, parameters).fetchall()
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(fields)
    writer.writerows(rows)
    content = output.getvalue()
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
    if os.environ.get("ARBICORE_PRODUCTION") == "1":
        missing = [name for name in ("ARBICORE_SESSION_SECRET", "ARBICORE_MASTER_KEY")
                   if not os.environ.get(name)]
        if DEFAULT_ADMIN_PASSWORD == "Admin@12345":
            missing.append("ARBICORE_ADMIN_PASSWORD (must not use the default)")
        if missing:
            raise RuntimeError("Production startup refused; configure " + ", ".join(missing))
    bot.load_live_config_if_present()
    bot.ORDER_INTENT_HOOK = persist_order_intent
    bot.ORDER_RESULT_HOOK = update_order_intent
    with state_lock:
        state["config"].update({
            "mode": bot.MODE,
            "execution_mode": bot.EXECUTION_MODE,
            "strategy": bot.TRADING_STRATEGY,
            "real_trading_enabled": bot.REAL_TRADING_ENABLED,
            "sandbox_mode": bot.SANDBOX_MODE,
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
    if os.environ.get("ARBICORE_DEV_SERVER") == "1":
        app.run(host=host, port=chosen_port, debug=False)
        return
    try:
        from waitress import serve as waitress_serve
    except ImportError:
        print("  WARNING: Waitress is not installed; using Flask development server.")
        print("  Run: pip install -r requirements.txt")
        app.run(host=host, port=chosen_port, debug=False)
        return
    waitress_serve(app, host=host, port=chosen_port, threads=8,
                   channel_timeout=120, clear_untrusted_proxy_headers=True)


if __name__ == "__main__":
    bootstrap()
    serve()
