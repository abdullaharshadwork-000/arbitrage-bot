from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import arbitrage_bot as bot
from arbicore.paper import PaperAccount


def test_capital_shared_across_pairs_and_venues():
    account = PaperAccount(["a", "b"], ["BTC/USDT", "ETH/USDT"], 20000,
                           {"BTC/USDT": 100, "ETH/USDT": 10}, "cross_exchange")
    assert account.total_value({}) == pytest.approx(20000)
    assert sum(sum(c.values()) for c in account.usdt.values()) == 10000


def test_snapshot_restores_holdings_but_uses_new_prices():
    account = PaperAccount(["a"], ["BTC/USDT"], 20000,
                           {"BTC/USDT": 100}, "cross_exchange")
    restored = PaperAccount(["a"], ["BTC/USDT"], 20000,
                            {"BTC/USDT": 110}, "cross_exchange", account.snapshot())
    assert restored.total_value({}) == 21000
    assert restored.initial == 20000


def test_triangular_execution_mutates_wallet():
    prices = {"BTC/USDT": 100, "ETH/BTC": .1, "ETH/USDT": 11}
    account = PaperAccount(["a"], list(prices), 20000, prices, "triangular")
    client = Mock()
    client.precisionMode = 4
    client.market.return_value = {"precision": {"amount": .00000001}}
    client.fetch_order_book.side_effect = lambda symbol, limit: {
        "asks": [[prices[symbol], 100000]], "bids": [[prices[symbol], 100000]]}
    cfg = {"mode": "live", "strategy": "triangular", "trade_size": 100,
           "fee": .001, "max_slippage": .25, "min_profit": .15}
    candidate = {"buy_exchange": "a", "cycle": {"symbols": list(prices), "prices": prices}}
    result, report = account.execute(cfg, candidate, SimpleNamespace(clients={"a": client}))
    assert result > 100
    assert account.total_value(prices) == pytest.approx(20000 + report["realized_profit"], abs=.00001)
    assert account.profit == report["realized_profit"]


def test_unverified_fee_blocks_and_zero_fee_is_valid():
    engine = bot.RealExecutionEngine.__new__(bot.RealExecutionEngine)
    client = SimpleNamespace(fetch_trading_fee=lambda symbol: {})
    engine.clients = {"a": client}
    with pytest.raises(RuntimeError, match="Verified trading fee unavailable"):
        engine.taker_fee("a")
    client.fetch_trading_fee = lambda symbol: {"taker": 0}
    assert engine.taker_fee("a") == 0
    client.fetch_trading_fee = lambda symbol: {}
    with patch("arbitrage_bot.time.monotonic", return_value=engine.fee_checked_at[("a", "BTC/USDT")] + 301):
        with pytest.raises(RuntimeError, match="Verified trading fee unavailable"):
            engine.taker_fee("a")
