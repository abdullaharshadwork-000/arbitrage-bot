"""Offline, deterministic candle replay for the automatic trend strategy.

Run ``python -m arbicore.signal_validation candles.json`` with a JSON array of
closed, consecutive one-minute [timestamp_ms, open, high, low, close, volume]
candles. No exchange client, credentials, network connection or order is used.

Decisions use only the previous 80 completed candles; entries occur at the next
open with adverse slippage. If a candle touches both stop and target, the stop
wins. Gaps through a stop fill at the worse opening price. A trailing high is
activated only after that candle closes (OHLC does not reveal tick ordering).
Fees apply to both sides, quantities round down, and previous-bar base volume
caps entry participation. Exit liquidity is NOT available from OHLC: exits are
assumed fully executable with the configured slippage. This is a sensitivity
tool, not an exchange simulator, profitability proof, or live-readiness gate.
"""

import argparse
import json
import math
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .money import floor_to_step
from .signals import analyse, evaluate_position


@dataclass(frozen=True)
class ReplaySettings:
    initial_balance: float = 20000
    trade_size: float = 100
    maximum_position: float = 100
    maximum_daily_loss: float = 50
    risk_fraction: float = .0025
    fee_rate: float = .001
    slippage_bps: float = 10
    quantity_step: float = .000001
    minimum_notional: float = 10
    max_entry_volume_fraction: float = .01
    max_trades_per_hour: int = 12
    cooldown_seconds: int = 300

    def validate(self):
        for name, value in asdict(self).items():
            if isinstance(value, bool) or not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
        for name in ("initial_balance", "trade_size", "maximum_position",
                     "maximum_daily_loss", "quantity_step", "minimum_notional"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        for name, maximum in (("fee_rate", .05), ("slippage_bps", 1000)):
            if not 0 <= getattr(self, name) <= maximum:
                raise ValueError(f"Invalid {name}")
        if not 0 < self.risk_fraction <= .01:
            raise ValueError("risk_fraction must be between 0 and 0.01")
        if not 0 < self.max_entry_volume_fraction <= 1:
            raise ValueError("max_entry_volume_fraction must be between 0 and 1")
        if not isinstance(self.max_trades_per_hour, int) or not 1 <= self.max_trades_per_hour <= 3600:
            raise ValueError("max_trades_per_hour must be an integer between 1 and 3600")
        if not isinstance(self.cooldown_seconds, int) or self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be a nonnegative integer")


def _decimal(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Expected a finite number") from exc
    if not result.is_finite():
        raise ValueError("Expected a finite number")
    return result


def _candles(rows):
    result = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            raise ValueError("Expected OHLCV candle rows")
        t, o, h, l, c, v = [float(_decimal(value)) for value in row[:6]]
        if not all(math.isfinite(value) for value in (t, o, h, l, c, v)):
            raise ValueError("Candle value exceeds supported range")
        if t < 0 or t % 60000 or min(o, h, l, c) <= 0 or v < 0 or not l <= min(o, c) <= max(o, c) <= h:
            raise ValueError("Invalid one-minute OHLCV candle")
        if result and t - result[-1][0] != 60000:
            raise ValueError("Candles must be consecutive, ordered and unique")
        result.append([int(t), o, h, l, c, v])
    if not result:
        raise ValueError("No candles supplied")
    return result


def replay(candles, settings=None):
    """Return a JSON-serializable report; do not mutate candles or settings.

    Include at least 35 warmup candles before the evaluation interval. Supply
    only completed candles from one market, with volume expressed in base units.
    The final open position is liquidated at the last close with fees/slippage,
    explicitly recorded as ``end_of_data`` instead of hiding an unrealized loss.
    """
    cfg = settings or ReplaySettings()
    cfg.validate()
    rows = _candles(candles)
    fee, slip = _decimal(cfg.fee_rate), _decimal(cfg.slippage_bps) / 10000
    cash = initial = _decimal(cfg.initial_balance)
    position = None
    fees = Decimal(0)
    trades, equity_curve, entry_times = [], [], []
    decisions = {"buy": 0, "sell": 0, "wait": 0}
    blocked = {"cooldown": 0, "daily_loss": 0, "hourly_limit": 0, "size_or_volume": 0}
    cooldown_until, day, day_pnl = 0, None, Decimal(0)
    day_start = peak = initial
    maximum_drawdown = Decimal(0)
    maximum_drawdown_pct = Decimal(0)
    ambiguous_candles = 0

    def equity(price):
        proceeds = position["quantity"] * _decimal(price) * (1 - slip) * (1 - fee) if position else 0
        return cash + proceeds

    def record_equity(timestamp, price):
        nonlocal peak, maximum_drawdown, maximum_drawdown_pct
        value = equity(price)
        peak = max(peak, value)
        drawdown = peak - value
        maximum_drawdown = max(maximum_drawdown, drawdown)
        maximum_drawdown_pct = max(maximum_drawdown_pct, drawdown / peak * 100)
        equity_curve.append({"timestamp_ms": timestamp, "equity": float(value)})

    def close_position(price, reason, timestamp):
        nonlocal cash, position, fees, day_pnl, cooldown_until
        fill = _decimal(price) * (1 - slip)
        gross = position["quantity"] * fill
        exit_fee = gross * fee
        proceeds = gross - exit_fee
        profit = proceeds - position["cost"]
        cash += proceeds
        fees += exit_fee
        day_pnl += profit
        trades.append({"entry_time_ms": int(position["opened_at"] * 1000),
                       "exit_time_ms": timestamp, "signal_time_ms": position["signal_time_ms"],
                       "entry_price": position["entry"], "exit_price": float(fill),
                       "quantity": float(position["quantity"]), "cost": float(position["cost"]),
                       "fees": float(position["entry_fee"] + exit_fee),
                       "profit": float(profit), "exit_reason": reason})
        position = None
        cooldown_until = timestamp / 1000 + cfg.cooldown_seconds

    for index, row in enumerate(rows):
        timestamp, opening, high, low, closing, _ = row
        now = timestamp / 1000
        if timestamp // 86400000 != day:
            day = timestamp // 86400000
            day_pnl, day_start = Decimal(0), equity(opening)
        # The current candle must not influence an entry made at its opening.
        report = analyse(rows[max(0, index - 80):index], timestamp)
        decisions[report["action"]] += 1
        started_with_position = position is not None
        if position:
            reason = evaluate_position(position, opening, report, now)
            reference = opening
            if day_start - equity(opening) >= _decimal(cfg.maximum_daily_loss):
                reason = "daily_equity_loss"
            if not reason:
                protective = position["stop"]
                protective_reason = "stop_loss"
                if position["high"] >= position["entry"] * (1 + position["stop_fraction"]):
                    trailing = position["high"] * (1 - position["stop_fraction"])
                    if trailing > protective:
                        protective, protective_reason = trailing, "trailing_stop"
                if low <= protective:
                    ambiguous_candles += high >= position["target"]
                    reason, reference = protective_reason, min(opening, protective)
                elif high >= position["target"]:
                    record_equity(timestamp, low)
                    reason, reference = "take_profit", position["target"]
            if reason:
                close_position(reference, reason, timestamp)
            else:
                # Record adverse excursion; the candle's ordering is not known.
                record_equity(timestamp, low)
                position["high"] = max(position["high"], high)
        if not position and not started_with_position and report["action"] == "buy":
            entry_times = [t for t in entry_times if now - t < 3600]
            daily_loss = max(Decimal(0), day_start - equity(opening), -day_pnl)
            if now < cooldown_until:
                blocked["cooldown"] += 1
            elif daily_loss >= _decimal(cfg.maximum_daily_loss):
                blocked["daily_loss"] += 1
            elif len(entry_times) >= cfg.max_trades_per_hour:
                blocked["hourly_limit"] += 1
            else:
                fill = _decimal(opening) * (1 + slip)
                stop_fraction = min(.03, max(.005, 2 * report["atr14"] / float(fill)))
                remaining = _decimal(cfg.maximum_daily_loss) - daily_loss
                risk_budget = min(equity(opening) * _decimal(cfg.risk_fraction), remaining)
                budget = min(cash, _decimal(cfg.trade_size), _decimal(cfg.maximum_position),
                             risk_budget / (_decimal(stop_fraction) + 2 * fee + 2 * slip))
                volume_cap = _decimal(rows[index - 1][5]) * _decimal(cfg.max_entry_volume_fraction)
                quantity = floor_to_step(min(budget / (fill * (1 + fee)), volume_cap),
                                         _decimal(cfg.quantity_step))
                notional, cost = quantity * fill, quantity * fill * (1 + fee)
                if quantity <= 0 or notional < _decimal(cfg.minimum_notional) or cost > cash:
                    blocked["size_or_volume"] += 1
                else:
                    cash -= cost
                    fees += notional * fee
                    entry_times.append(now)
                    position = {"quantity": quantity, "cost": cost, "entry_fee": notional * fee,
                                "entry": float(fill), "high": float(fill), "opened_at": now,
                                "signal_time_ms": report["candle_time"],
                                "stop_fraction": stop_fraction,
                                "stop": float(fill) * (1 - stop_fraction),
                                "target": float(fill) * (1 + 2 * stop_fraction + 2 * float(fee))}
                    # Newly opened positions are also exposed during this candle.
                    if low <= position["stop"]:
                        ambiguous_candles += high >= position["target"]
                        close_position(min(opening, position["stop"]), "stop_loss", timestamp)
                    elif high >= position["target"]:
                        record_equity(timestamp, low)
                        close_position(position["target"], "take_profit", timestamp)
                    else:
                        record_equity(timestamp, low)
                        position["high"] = max(position["high"], high)
        record_equity(timestamp + 60000, closing)
    if position:
        close_position(rows[-1][4], "end_of_data", rows[-1][0] + 60000)
        record_equity(rows[-1][0] + 60000, rows[-1][4])
    gains = sum(_decimal(t["profit"]) for t in trades if t["profit"] > 0)
    losses = -sum(_decimal(t["profit"]) for t in trades if t["profit"] < 0)
    winning = sum(t["profit"] > 0 for t in trades)
    return {"settings": asdict(cfg), "candles": len(rows),
            "first_candle_time_ms": rows[0][0], "last_close_time_ms": rows[-1][0] + 60000,
            "initial_balance": float(initial), "final_balance": float(cash),
            "net_profit": float(cash - initial), "return_pct": float((cash / initial - 1) * 100),
            "fees_paid": float(fees), "trades_count": len(trades), "winning_trades": winning,
            "losing_trades": sum(t["profit"] < 0 for t in trades),
            "win_rate_pct": winning / len(trades) * 100 if trades else None,
            "profit_factor": float(gains / losses) if losses else None,
            "maximum_drawdown": float(maximum_drawdown),
            "maximum_drawdown_pct": float(maximum_drawdown_pct),
            "worst_trade": min((t["profit"] for t in trades), default=None),
            "ambiguous_stop_target_candles": ambiguous_candles,
            "signals": decisions, "blocked_entries": blocked,
            "trades": trades, "equity_curve": equity_curve,
            "limitations": ["Historical replay is not evidence of future profitability or live readiness.",
                            "Exit liquidity, outages and partial fills cannot be inferred from candles.",
                            "Stops win stop/target ambiguity; trailing highs activate next candle.",
                            "Intrabar exit timestamps identify a candle, not an exact fill time.",
                            "Final positions are forcibly liquidated at the last close."]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candles", type=Path, help="Local JSON array of completed one-minute OHLCV rows")
    parser.add_argument("--fee-rate", type=float, default=.001)
    parser.add_argument("--slippage-bps", type=float, default=10)
    parser.add_argument("--initial-balance", type=float, default=20000)
    args = parser.parse_args(argv)
    try:
        with args.candles.open(encoding="utf-8") as source:
            rows = json.load(source)
        report = replay(rows, ReplaySettings(fee_rate=args.fee_rate,
                        slippage_bps=args.slippage_bps, initial_balance=args.initial_balance))
    except (OSError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
