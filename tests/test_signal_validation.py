"""Replay tests deliberately include losses, ambiguity and execution costs."""

from dataclasses import replace
import json

import pytest

from arbicore import signal_validation as validation


def rows(count=40):
    return [[i * 60000, 100, 100.1, 99.9, 100, 1000] for i in range(count)]


def force_entry(monkeypatch, every=False):
    def analyse(candles, now_ms):
        assert all(row[0] + 60000 <= now_ms for row in candles)
        return {"action": "buy" if len(candles) >= 35 and (every or len(candles) == 35) else "wait",
                "atr14": .5, "candle_time": candles[-1][0] if candles else None}
    monkeypatch.setattr(validation, "analyse", analyse)


def test_replay_is_deterministic_and_never_reads_current_candle(monkeypatch):
    force_entry(monkeypatch)
    candles = rows()
    first = validation.replay(candles)
    assert first == validation.replay(candles)
    assert candles == rows()  # Neither input data nor config is mutated.
    trade = first["trades"][0]
    assert trade["signal_time_ms"] + 60000 == trade["entry_time_ms"]
    assert trade["exit_reason"] == "end_of_data"
    assert first["final_balance"] == pytest.approx(20000 + sum(t["profit"] for t in first["trades"]))


def test_fees_and_adverse_slippage_turn_flat_price_trade_into_loss(monkeypatch):
    force_entry(monkeypatch)
    free = validation.replay(rows(), validation.ReplaySettings(fee_rate=0, slippage_bps=0))
    costly = validation.replay(rows())
    assert free["net_profit"] == pytest.approx(0)
    assert costly["net_profit"] < 0
    assert costly["losing_trades"] == 1
    assert costly["fees_paid"] > 0
    assert costly["win_rate_pct"] == 0
    assert costly["maximum_drawdown"] >= -costly["net_profit"]
    assert costly["trades"][0]["exit_price"] < costly["trades"][0]["entry_price"]


def test_stop_wins_ambiguous_entry_candle(monkeypatch):
    force_entry(monkeypatch)
    candles = rows(36)
    candles[-1][2:5] = [110, 90, 100]
    report = validation.replay(candles)
    assert report["ambiguous_stop_target_candles"] == 1
    assert report["trades"][0]["exit_reason"] == "stop_loss"
    assert report["net_profit"] < 0


def test_gap_through_stop_fills_at_worse_open_not_stop(monkeypatch):
    force_entry(monkeypatch)
    candles = rows(37)
    candles[-1][1:5] = [90, 91, 89, 90]
    report = validation.replay(candles)
    trade = report["trades"][0]
    assert trade["exit_reason"] == "stop_loss"
    assert trade["exit_price"] == pytest.approx(90 * .999)
    assert report["maximum_drawdown_pct"] > 0


def test_loss_cap_blocks_new_entries_after_gap(monkeypatch):
    force_entry(monkeypatch, every=True)
    candles = rows(38)
    candles[36][1:5] = [50, 51, 49, 50]
    report = validation.replay(candles, validation.ReplaySettings(maximum_daily_loss=1, cooldown_seconds=0))
    assert report["trades_count"] == 1
    assert report["blocked_entries"]["daily_loss"] == 1
    # The gap must be reported honestly, not clamped to the nominal loss cap.
    assert report["net_profit"] < -1


def test_quantity_rounds_down_and_volume_caps_entry(monkeypatch):
    force_entry(monkeypatch)
    candles = rows(36)
    candles[34][5] = 15.5
    cfg = validation.ReplaySettings(quantity_step=.1)
    report = validation.replay(candles, cfg)
    assert report["trades"][0]["quantity"] == .1
    assert report["trades"][0]["cost"] <= 100
    candles[34][5] = .01
    blocked = validation.replay(candles, cfg)
    assert blocked["trades_count"] == 0
    assert blocked["blocked_entries"]["size_or_volume"] == 1


def test_profit_and_loss_are_both_retained(monkeypatch):
    force_entry(monkeypatch)
    candles = rows(37)
    candles[-1][1:5] = [100, 104, 100, 103]
    report = validation.replay(candles)
    assert report["trades"][0]["exit_reason"] == "take_profit"
    assert report["net_profit"] > 0
    assert report["profit_factor"] is None  # No infinity/NaN or implied certainty.
    assert report["winning_trades"] == 1
    assert report["limitations"]


def test_real_signal_can_warm_up_and_generate_an_entry():
    candles = []
    price = 100
    for i in range(81):
        opening = price
        price += -.5 if i % 4 == 0 else .3
        candles.append([i * 60000, opening, max(opening, price) + .05,
                        min(opening, price) - .05, price, 150 if i == 79 else 100])
    report = validation.replay(candles)
    assert report["signals"]["buy"] == 1
    assert report["trades_count"] == 1
    assert report["trades"][0]["entry_time_ms"] == 80 * 60000


def test_prior_candle_high_arms_trailing_stop(monkeypatch):
    force_entry(monkeypatch)
    candles = rows(38)
    candles[36][1:5] = [100, 101.5, 99.9, 101.4]
    candles[37][1:5] = [101.4, 101.5, 100, 100.1]
    report = validation.replay(candles)
    assert report["trades"][0]["exit_reason"] == "trailing_stop"


def test_maximum_hold_time_exits_even_without_signal(monkeypatch):
    force_entry(monkeypatch)
    report = validation.replay(rows(67))
    assert report["trades"][0]["exit_reason"] == "maximum_hold_time"
    assert report["trades"][0]["exit_time_ms"] == 65 * 60000


def test_cli_reads_local_json_and_emits_finite_report(tmp_path, capsys):
    candle_file = tmp_path / "candles.json"
    candle_file.write_text(json.dumps(rows(20)), encoding="utf-8")
    validation.main([str(candle_file), "--fee-rate", ".002", "--slippage-bps", "20"])
    report = json.loads(capsys.readouterr().out)
    assert report["settings"]["fee_rate"] == .002
    assert report["settings"]["slippage_bps"] == 20
    assert report["last_close_time_ms"] == 20 * 60000


def test_warmup_has_no_trades_or_fabricated_win_rate():
    report = validation.replay(rows(20))
    assert report["final_balance"] == 20000
    assert report["trades_count"] == 0
    assert report["win_rate_pct"] is None
    assert report["profit_factor"] is None


@pytest.mark.parametrize("bad", [[], [[0, 100]], [[0, 100, 101, 99, float("nan"), 100]],
                                 [[1, 100, 101, 99, 100, 100]],
                                 [[0, 100, 101, 102, 100, 100]]])
def test_malformed_candles_rejected_even_before_warmup(bad):
    with pytest.raises(ValueError):
        validation.replay(bad)


def test_candle_gap_rejected_before_warmup():
    candles = rows(3)
    candles[-1][0] += 60000
    with pytest.raises(ValueError, match="consecutive"):
        validation.replay(candles)


@pytest.mark.parametrize("change", [{"fee_rate": -.01}, {"slippage_bps": -1},
                                    {"risk_fraction": .9}, {"quantity_step": 0},
                                    {"initial_balance": float("inf")},
                                    {"maximum_daily_loss": 0}, {"max_trades_per_hour": 1.5},
                                    {"max_entry_volume_fraction": 1.5}])
def test_unsafe_or_invalid_replay_configuration_rejected(change):
    with pytest.raises(ValueError):
        validation.replay(rows(), replace(validation.ReplaySettings(), **change))
