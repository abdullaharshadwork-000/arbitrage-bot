"""Offline regressions for market analysis and account-specific chart context."""

import copy
import importlib
import json
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from arbicore import market
from arbicore.paper import PaperAccount


NOW = 1_800_000_000  # Aligned to all supported chart intervals.


def candles(timeframe="1m", count=40, end=NOW, descending=False):
    step = market.TIMEFRAMES[timeframe]
    rows = []
    for index in range(count):
        price = 200 - index if descending else 100 + index
        rows.append([(end - (count - index) * step) * 1000,
                     price, price + 2, price - 2, price + 1, 10])
    return rows


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


class PublicExchange:
    """Only the public candle API exists; private/order APIs are unavailable."""

    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.error = None

    def fetch_ohlcv(self, symbol, timeframe, limit):
        self.calls.append((symbol, timeframe, limit))
        if self.error:
            raise self.error
        step = market.TIMEFRAMES[timeframe]
        return candles(timeframe, end=int(self.clock.now // step) * step, descending=True)


@pytest.mark.parametrize("venue", ["binance", "kucoin", "okx", "bybit"])
def test_chart_clients_are_credential_free_spot_only_without_retries(venue):
    client = market.public_client(venue)
    assert not client.apiKey and not client.secret and not client.password
    assert client.options["defaultType"] == "spot"
    assert client.options["fetchMarkets"]["types"] == ["spot"]
    assert client.options["fetchCurrencies"] is False
    assert client.maxRetriesOnFailure == 0


@pytest.fixture
def overview():
    clock = Clock()
    exchange = PublicExchange(clock)
    factory = Mock(return_value=exchange)
    service = market.MarketOverview(["binance", "okx"], ["BTC/USDT", "ETH/USDT"],
                                    factory=factory, clock=clock, ttl=5)
    return service, exchange, clock, factory


@pytest.mark.parametrize("raw", [None, {}, [], [None], [[NOW * 1000, 1, 2]],
                                     candles(count=1) * 501])
def test_candle_rows_rejects_empty_malformed_or_excessive_history(raw):
    with pytest.raises(ValueError):
        market.candle_rows(raw, "1m", NOW)


@pytest.mark.parametrize("column,value", [
    (0, -60_000), (0, NOW * 1000 + 60_000), (0, NOW * 1000 - 1),
    (1, True), (1, 0), (2, float("inf")), (3, float("nan")),
    (4, -1), (5, -1), (2, 99), (3, 102),
])
def test_candle_rows_rejects_invalid_ohlcv(column, value):
    raw = candles(count=1)
    raw[0][column] = value
    with pytest.raises(ValueError):
        market.candle_rows(raw, "1m", NOW)


@pytest.mark.parametrize("kind", ["duplicate", "gap", "reversed"])
def test_candle_rows_rejects_broken_sequence(kind):
    raw = candles(count=3)
    if kind == "duplicate":
        raw[1][0] = raw[0][0]
    elif kind == "gap":
        del raw[1]
    else:
        raw.reverse()
    with pytest.raises(ValueError, match="sequence"):
        market.candle_rows(raw, "1m", NOW)


def test_chart_can_include_live_candle_but_signal_uses_only_closed_candles():
    raw = candles(descending=True)
    completed = market.market_payload(raw, raw, "1m", NOW + 20)
    raw.append([NOW * 1000, 1_000, 1_002, 998, 1_001, 100_000])
    with_live = market.market_payload(raw, raw, "1m", NOW + 20)

    assert with_live["signal"] == completed["signal"]
    assert with_live["signal"]["action"] == "sell"
    assert with_live["candles"][-2]["closed"] is True
    assert with_live["candles"][-1]["closed"] is False
    assert with_live["last_price"] == 1_001
    assert with_live["last_closed_candle"] == NOW - 60
    assert with_live["execution_allowed"] is False


@pytest.mark.parametrize("age,fresh", [(0, True), (60, True), (60.001, False)])
def test_minute_feed_staleness_boundary_disables_decisions(age, fresh):
    raw = candles(descending=True)
    payload = market.market_payload(raw, raw, "1m", NOW + age)
    assert payload["fresh"] is fresh
    assert payload["signal"]["action"] == ("sell" if fresh else "wait")
    if not fresh:
        assert payload["regime"] == "Unavailable"
        assert "stale" in payload["signal"]["reason"].lower()


def test_insufficient_completed_candles_waits_for_warmup():
    raw = candles(count=34)
    payload = market.market_payload(raw, raw, "1m", NOW)
    assert payload["signal"]["action"] == "wait"
    assert payload["regime"] == "Warming up"


@pytest.mark.parametrize("timeframe", ["5m", "15m"])
def test_stale_chart_cannot_appear_fresh_because_minute_feed_is_current(timeframe):
    chart = candles(timeframe, end=NOW - 2 * market.TIMEFRAMES[timeframe])
    payload = market.market_payload(chart, candles(descending=True), timeframe, NOW)
    assert payload["fresh"] is False
    assert payload["signal"]["action"] == "wait"
    assert payload["regime"] == "Unavailable"


def test_chart_timeframe_does_not_change_one_minute_decision(overview):
    service, exchange, _, _ = overview
    minute = service.snapshot("binance", "BTC/USDT", "1m")
    five = service.snapshot("binance", "BTC/USDT", "5m")
    assert five["signal"] == minute["signal"]
    assert five["chart_timeframe"] == "5m"
    assert five["decision_timeframe"] == "1m"
    assert exchange.calls == [("BTC/USDT", "1m", 180),
                              ("BTC/USDT", "5m", 180), ("BTC/USDT", "1m", 80)]


def test_cache_reuses_public_data_without_sharing_response_mutations(overview):
    service, exchange, clock, factory = overview
    first = service.snapshot("binance", "BTC/USDT")
    original_close = first["candles"][0]["close"]
    first["candles"][0]["close"] = -999
    first["signal"]["action"] = "private-account-marker"
    first["account"] = {"position": "private-account-marker"}
    clock.now += 4
    cached = service.snapshot("binance", "BTC/USDT")
    assert len(exchange.calls) == 1
    assert cached["candles"][0]["close"] == original_close
    assert cached["signal"]["action"] == "sell"
    assert "account" not in cached

    clock.now += 1
    assert service.snapshot("binance", "BTC/USDT")["as_of"] == clock.now
    assert len(exchange.calls) == 2
    factory.assert_called_once_with("binance")


def test_cached_signal_is_suppressed_when_feed_ages_past_freshness_limit(overview):
    service, exchange, clock, _ = overview
    clock.now = NOW + 59
    first = service.snapshot("binance", "BTC/USDT")
    assert first["fresh"] is True
    assert first["signal"]["action"] == "sell"
    clock.now += 2  # Still within cache TTL, but past the candle freshness limit.
    expired = service.snapshot("binance", "BTC/USDT")
    assert len(exchange.calls) == 1
    assert expired["fresh"] is False
    assert expired["signal"]["action"] == "wait"
    assert expired["execution_allowed"] is False


def test_cache_is_scoped_by_venue_pair_and_timeframe(overview):
    service, exchange, _, factory = overview
    for venue, pair, timeframe in [("binance", "BTC/USDT", "1m"),
                                   ("okx", "BTC/USDT", "1m"),
                                   ("binance", "ETH/USDT", "1m"),
                                   ("binance", "BTC/USDT", "15m")]:
        result = service.snapshot(venue, pair, timeframe)
        assert (result["exchange"], result["symbol"], result["chart_timeframe"]) == (venue, pair, timeframe)
    assert len(exchange.calls) == 5
    assert factory.call_count == 2


@pytest.mark.parametrize("venue,pair,timeframe", [("unknown", "BTC/USDT", "1m"),
    ("binance", "UNLISTED/USDT", "1m"), ("binance", "BTC/USDT", "1h")])
def test_unsupported_market_is_rejected_before_client_creation(overview, venue, pair, timeframe):
    service, _, _, factory = overview
    with pytest.raises(ValueError, match="Unsupported"):
        service.snapshot(venue, pair, timeframe)
    factory.assert_not_called()


def test_exchange_failure_discards_expired_data_and_sanitizes_error(overview):
    service, exchange, clock, _ = overview
    assert service.snapshot("binance", "BTC/USDT")["available"]
    clock.now += 5
    exchange.error = RuntimeError("https://private.invalid/?signature=secret-marker")
    unavailable = service.snapshot("binance", "BTC/USDT")
    assert unavailable["available"] is False
    assert unavailable["fresh"] is False
    assert unavailable["execution_allowed"] is False
    assert unavailable["signal"]["action"] == "wait"
    assert unavailable["candles"] == []
    assert unavailable["error"] == "RuntimeError"
    assert "secret-marker" not in json.dumps(unavailable)


@pytest.fixture
def server_context(tmp_path, monkeypatch, overview):
    # Import-time database initialization must never touch the operator's DB.
    database = tmp_path / "market-tests.db"
    monkeypatch.setenv("ARBICORE_DB", str(database))
    server = importlib.import_module("server")
    monkeypatch.setattr(server, "DB_FILE", database)
    server.initialize_database()
    monkeypatch.setattr(server, "state", copy.deepcopy(server.state))
    server.state.update(owner_user_id=None, current_user=None, running=False)
    monkeypatch.setitem(server.app.config, "TESTING", False)
    monkeypatch.setattr(server, "market_overview", overview[0])
    monkeypatch.setattr(server, "wallet", None)
    monkeypatch.setattr(server, "get_readiness", lambda: {"ready": False, "message": "offline test"})
    today = datetime.now(timezone.utc).date().isoformat()
    monkeypatch.setattr(server.risk_manager, "export_state", lambda: {
        "day": today, "realized_today": -75, "halted": True, "halt_reason": "owner-only halt"})
    return server


def signed_client(server, user_id, revoked=False):
    """Create an actual database-backed session without login/start side effects."""
    with server.db() as connection:
        connection.execute(
            "INSERT INTO users(id,username,email,password_hash,role,created_at) VALUES(?,?,?,?,?,?)",
            (user_id, f"market-{user_id}", f"market-{user_id}@localhost", "unused", "trader", "2026-01-01"))
        connection.execute(
            "INSERT INTO auth_sessions(session_id,user_id,created_at,expires_at,revoked_at) VALUES(?,?,?,?,?)",
            (f"market-session-{user_id}", user_id, "2026-01-01", 9_999_999_999,
             "2026-01-02" if revoked else None))
    client = server.app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = user_id
        session["session_id"] = f"market-session-{user_id}"
    return client


@pytest.mark.parametrize("revoked", [False, True])
def test_overview_requires_valid_auth_before_fetching_market(server_context, overview, revoked):
    server = server_context
    client = signed_client(server, 101, revoked=True) if revoked else server.app.test_client()
    response = client.get("/api/market/overview")
    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"
    overview[3].assert_not_called()


def test_overview_rejects_unsupported_query(server_context, overview):
    client = signed_client(server_context, 101)
    response = client.get("/api/market/overview?timeframe=1h")
    assert response.status_code == 400
    overview[3].assert_not_called()


def test_account_overview_is_not_http_cacheable(server_context):
    client = signed_client(server_context, 101)
    response = client.get("/api/market/overview")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"


@pytest.mark.parametrize("feed_failure", [False, True])
def test_stale_or_unavailable_feed_cannot_suggest_hold_or_exit_for_open_position(server_context, overview, monkeypatch, feed_failure):
    server = server_context
    client = signed_client(server, 101)
    server.state["owner_user_id"] = 101
    server.state["config"].update(strategy="signal_trend", execution_mode="paper", mode="live")
    server.wallet = PaperAccount(["binance"], ["BTC/USDT"], 1_000,
                                 {"BTC/USDT": 100}, "signal_trend")
    server.wallet.signal_state = {"position": {"symbol": "BTC/USDT", "exchange": "binance"}}
    if feed_failure:
        overview[1].error = RuntimeError("offline exchange")
    else:
        monkeypatch.setattr(overview[1], "fetch_ohlcv", Mock(return_value=candles(end=NOW - 120)))
    response = client.get("/api/market/overview")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["account"]["position"]["symbol"] == "BTC/USDT"
    assert payload["fresh"] is False
    assert payload["suggestion"] == "WAIT"
    assert payload["execution_allowed"] is False


@pytest.mark.parametrize("realized", ["not-numeric", None, float("nan")])
def test_malformed_saved_risk_amount_fails_closed_without_breaking_chart(server_context, realized):
    server = server_context
    client = signed_client(server, 102)
    today = datetime.now(timezone.utc).date().isoformat()
    with server.db() as connection:
        connection.execute("INSERT INTO risk_states(user_id,payload,updated_at) VALUES(?,?,?)", (
            102, json.dumps({"day": today, "realized_today": realized}), today))
    response = client.get("/api/market/overview")
    assert response.status_code == 200
    account = response.get_json()["account"]
    assert account["readiness"]["ready"] is False
    assert account["halted"] is True
    assert account["daily_loss_remaining"] is None
    assert account["daily_realized_pnl"] is None


def test_shared_market_cache_keeps_account_risk_positions_and_worker_ownership_isolated(server_context, overview):
    server = server_context
    owner = signed_client(server, 101)
    visitor = signed_client(server, 102)
    today = datetime.now(timezone.utc).date().isoformat()
    server.state.update(owner_user_id=101, running=True)
    server.state["config"].update(strategy="signal_trend", execution_mode="paper", mode="live", max_daily_loss=100)
    server.wallet = PaperAccount(["binance"], ["BTC/USDT"], 1_000,
                                 {"BTC/USDT": 100}, "signal_trend")
    owner_position = {"symbol": "BTC/USDT", "exchange": "binance", "quantity": "0.3"}
    server.wallet.signal_state = {"position": owner_position, "day": today, "day_pnl": -12}
    visitor_position = {"symbol": "ETH/USDT", "exchange": "okx", "quantity": "0.9"}
    with server.db() as connection:
        connection.execute("INSERT INTO user_configs(user_id,payload,updated_at) VALUES(?,?,?)", (
            102, json.dumps({"config": {"strategy": "signal_trend", "execution_mode": "paper",
                "mode": "live", "exchanges": ["okx"], "max_daily_loss": 25}}), today))
        connection.execute("INSERT INTO paper_accounts(user_id,mode,payload,updated_at) VALUES(?,?,?,?)", (
            102, "signal_paper", json.dumps({"signal_state": {
                "position": visitor_position, "day": today, "day_pnl": -3}}), today))
        connection.execute("INSERT INTO risk_states(user_id,payload,updated_at) VALUES(?,?,?)", (
            102, json.dumps({"day": today, "realized_today": -3, "halted": False}), today))

    first = owner.get("/api/market/overview").get_json()
    second = visitor.get("/api/market/overview").get_json()
    again = owner.get("/api/market/overview").get_json()

    assert first["account"]["position"] == owner_position
    assert first["account"]["daily_loss_remaining"] == 88
    assert first["account"]["halted"] is True
    assert first["account"]["running"] is True
    assert first["suggestion"] == "EXIT"
    assert second["account"]["position"] == visitor_position
    assert second["account"]["daily_loss_remaining"] == 22
    assert second["account"]["halted"] is False
    assert second["account"]["halt_reason"] == ""
    assert second["account"]["running"] is False
    assert second["account"]["readiness"]["ready"] is False
    assert second["suggestion"] == "WAIT"
    assert "does not open a short" in second["suggestion_note"]
    assert first["account"] == again["account"]
    assert first["candles"] == second["candles"]
    assert first["execution_allowed"] is second["execution_allowed"] is False
    assert len(overview[1].calls) == 1
    assert "account" not in overview[0].snapshot("binance", "BTC/USDT")
    assert server.state["owner_user_id"] == 101
    assert server.state["running"] is True
