from types import SimpleNamespace
from unittest.mock import patch

import pytest

from arbicore import signals
from arbicore.config import Settings
from arbicore.paper import PaperAccount


NOW = 60000.0


def candles():
    price = 100
    rows = []
    for i in range(80):
        previous = price
        price += -.5 if i % 4 == 0 else .3
        rows.append([(920 + i) * 60000, previous, max(previous, price) + .05,
                     min(previous, price) - .05, price, 150 if i == 79 else 100])
    return rows


def test_closed_candle_indicators_and_momentum():
    report = signals.analyse(candles(), NOW * 1000)
    assert report["action"] == "buy"
    assert report["momentum5_pct"] > 0 and report["momentum10_pct"] > 0
    assert 50 < report["rsi14"] < 78
    # An unfinished candle must never change the decision.
    unfinished = [NOW * 1000, 100, 200, 1, 2, 100000]
    assert signals.analyse(candles() + [unfinished], NOW * 1000) == report


def test_stale_duplicate_and_invalid_candles_block():
    with pytest.raises(ValueError, match="stale"):
        signals.analyse(candles(), NOW * 1000 + 180000)
    with pytest.raises(ValueError, match="duplicated"):
        signals.analyse(candles() + [candles()[-1]], NOW * 1000)
    bad = candles()
    bad[-1][4] = float("nan")
    with pytest.raises(ValueError, match="Malformed"):
        signals.analyse(bad, NOW * 1000)


def test_overheated_rise_does_not_force_an_entry():
    rows = [[(920+i)*60000, 100+i, 102+i, 99+i, 101+i, 150] for i in range(80)]
    assert signals.analyse(rows, NOW * 1000)["action"] == "wait"


def setup():
    account = PaperAccount(["venue"], ["BTC/USDT"], 20000,
                           {"BTC/USDT": 100}, "signal_trend")
    client = SimpleNamespace(precisionMode=4,
        market=lambda symbol: {"precision": {"amount": .000001}, "limits": {"cost": {"min": 10}}},
        fetch_ohlcv=lambda *args, **kwargs: candles(),
        fetch_order_book=lambda *args, **kwargs: {"asks": [[100, 100]], "bids": [[99.99, 100]]})
    cfg = {"execution_mode": "paper", "mode": "live", "trade_size": 100,
           "max_position_notional": 100, "max_daily_loss": 50, "fee": .001,
           "max_slippage": .25, "max_trades_per_hour": 12}
    return account, client, cfg


def test_entry_exit_fees_and_restart_are_accounted_for():
    account, client, cfg = setup()
    quote = {"BTC/USDT": {"venue": {"bid": 99.99, "ask": 100}}}
    with patch("arbicore.signals.time.sleep"):
        _, trade = signals.paper_tick(account, client, "venue", ["BTC/USDT"], quote, cfg, NOW)
        assert trade is None
        position = account.signal_state["position"]
        assert position and float(position["cost"]) <= 100
        assert account.total_value({"BTC/USDT": 100}) < 20000  # Actual entry fee.
        restored = PaperAccount(["venue"], ["BTC/USDT"], 20000, {}, "signal_trend", account.snapshot())
        assert restored.signal_state["position"] == position
        cash = restored.ledger.get("venue", "USDT")
        signals.paper_tick(restored, client, "venue", ["BTC/USDT"], quote, cfg, NOW + 1)
        assert restored.ledger.get("venue", "USDT") == cash  # No second entry.
        client.fetch_order_book = lambda *args, **kwargs: {"bids": [[95, 100]], "asks": [[95.01, 100]]}
        quote["BTC/USDT"]["venue"] = {"bid": 95, "ask": 95.01}
        restored.signal_cache.clear()
        client.fetch_ohlcv = lambda *args, **kwargs: pytest.fail("A stop exit must not wait for candle fetching")
        _, trade = signals.paper_tick(restored, client, "venue", ["BTC/USDT"], quote, cfg, NOW + 2)
        assert trade["exit_reason"] == "stop_loss"
        assert trade["profit_usdt"] < 0
        assert restored.signal_state["position"] is None
        assert restored.total_value({}) == pytest.approx(20000 + trade["profit_usdt"])


def test_real_execution_is_rejected_before_any_market_call():
    account, client, cfg = setup()
    cfg["execution_mode"] = "real"
    with pytest.raises(ValueError, match="paper"):
        signals.paper_tick(account, client, "venue", [], {}, cfg, NOW)
    problems = Settings(strategy="signal_trend", mode="live", execution_mode="real",
                        exchanges=("binance",)).validate()
    assert any("Signal trend" in p for p in problems)


def test_exit_time_and_trailing_rules():
    position = {"entry": 100, "high": 103, "stop": 99, "target": 110,
                "stop_fraction": .01, "opened_at": 0}
    assert signals.evaluate_position(dict(position), 101.5, {}, 100) == "trailing_stop"
    assert signals.evaluate_position(dict(position), 104, {}, 1800) == "maximum_hold_time"


@pytest.mark.parametrize("bad", [float("inf"), float("nan"), -1, 0])
def test_invalid_books_cannot_credit_wallet(bad):
    with pytest.raises(ValueError):
        signals.validate_book({"bids": [[bad, 10]], "asks": [[100, 10]]}, NOW)


def test_book_age_ordering_and_exchange_limits():
    with pytest.raises(ValueError, match="Stale"):
        signals.validate_book({"bids": [[99, 10]], "asks": [[100, 10]], "timestamp": NOW*1000-5000}, NOW)
    with pytest.raises(ValueError, match="Unordered"):
        signals.validate_book({"bids": [[98, 10], [99, 10]], "asks": [[100, 10]]}, NOW)
    from arbicore.money import D
    assert signals.market_limit_error({"limits": {"amount": {"min": 1}}}, D(.5), D(100))
    assert signals.market_limit_error({"limits": {"cost": {"max": 50}}}, D(1), D(100))


def test_equity_loss_closes_existing_position_and_cooldown_blocks_reentry():
    account, client, cfg = setup()
    quote = {"BTC/USDT": {"venue": {"bid": 99.99, "ask": 100}}}
    with patch("arbicore.signals.time.sleep"):
        signals.paper_tick(account, client, "venue", ["BTC/USDT"], quote, cfg, NOW)
        cfg["max_daily_loss"] = .01
        _, trade = signals.paper_tick(account, client, "venue", ["BTC/USDT"], quote, cfg, NOW+1)
        assert trade["exit_reason"] == "daily_equity_loss"
        signals.paper_tick(account, client, "venue", ["BTC/USDT"], quote, cfg, NOW+2)
        assert account.signal_state["position"] is None


def test_pause_during_fetch_prevents_entry():
    account, client, cfg = setup()
    quote = {"BTC/USDT": {"venue": {"bid": 99.99, "ask": 100}}}
    with patch("arbicore.signals.time.sleep"):
        reports, trade = signals.paper_tick(account, client, "venue", ["BTC/USDT"], quote, cfg, NOW,
                                            cancelled=lambda: True)
    assert trade is None
    assert account.signal_state.get("position") is None
    assert account.total_value({}) == 20000
    assert "stopped" in reports["BTC/USDT"]["reason"]
