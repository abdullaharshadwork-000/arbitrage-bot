from unittest.mock import Mock
import pytest
from arbitrage_bot import RealExecutionEngine


@pytest.fixture
def engine():
    result = RealExecutionEngine.__new__(RealExecutionEngine)
    result.clients = {name: Mock() for name in ("binance", "kucoin", "okx", "bybit")}
    result.markets = {name: {"BTC/USDT": {}} for name in result.clients}
    return result


@pytest.mark.parametrize("venue", ["binance", "kucoin", "okx", "bybit"])
def test_all_holdings_including_locked_unselected_coin(engine, venue):
    engine.clients[venue].fetch_balance.return_value = {
        "USDT": {"free": 100, "used": 20, "total": 120},
        "BTC": {"free": 0, "used": 2, "total": 2}, "info": {"secret": "never returned"}}
    engine.clients[venue].fetch_ticker.return_value = {"bid": 50}
    balances = engine.fetch_balances(venue)
    assert set(balances) == {"USDT", "BTC"}
    assert engine.value_balances_usdt(venue, balances)["total_usdt"] == 220
    assert balances["BTC"]["value_used_usdt"] == 100


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, None])
def test_invalid_holding_refused(engine, bad):
    engine.clients["binance"].fetch_balance.return_value = {
        "BTC": {"free": bad, "used": 0, "total": 1}}
    with pytest.raises(ValueError):
        engine.fetch_balances("binance")


def test_maps_and_unpriced_inventory(engine):
    engine.clients["okx"].fetch_balance.return_value = {
        "free": {"USDT": 100, "UNKNOWN": 1},
        "used": {"USDT": 0, "UNKNOWN": 0},
        "total": {"USDT": 100, "UNKNOWN": 1}}
    balances = engine.fetch_balances("okx")
    with pytest.raises(ValueError, match="Cannot value UNKNOWN"):
        engine.value_balances_usdt("okx", balances)


def test_inverse_valuation_and_zero_holdings(engine):
    engine.markets["bybit"] = {"USDT/COIN": {}}
    engine.clients["bybit"].fetch_ticker.return_value = {"ask": 2}
    balances = {"COIN": {"free": 4, "used": 0}, "UNLISTED": {"free": 0, "used": 0}}
    assert engine.value_balances_usdt("bybit", balances)["total_usdt"] == 2


def test_unrecognized_response_is_not_a_confirmed_empty_account(engine):
    engine.clients["binance"].fetch_balance.return_value = {}
    with pytest.raises(ValueError, match="no normalized holdings"):
        engine.fetch_balances("binance")
    engine.clients["binance"].fetch_balance.return_value = {"free": {}, "used": {}, "total": {}}
    assert engine.fetch_balances("binance")["USDT"]["total"] == 0
