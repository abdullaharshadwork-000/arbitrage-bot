"""Closed-candle trend signals and long-only paper position management.

Not a price predictor or a TradingView data connection. Never submits orders.
"""
import math
import time
from datetime import datetime, timezone

from . import books
from .money import D, floor_to_step


def ema(values, period):
    value = sum(values[:period]) / period
    alpha = 2 / (period + 1)
    for price in values[period:]:
        value += alpha * (price - value)
    return value


def analyse(candles, now_ms):
    """Ignore unfinished candles and fail closed on stale/gapped/bad data."""
    rows = []
    for row in candles:
        if len(row) < 6 or not all(math.isfinite(float(v)) for v in row[:6]):
            raise ValueError("Malformed candle data")
        t, o, h, l, c, v = map(float, row[:6])
        if min(o, h, l, c) <= 0 or v < 0 or not l <= min(o, c) <= max(o, c) <= h:
            raise ValueError("Invalid OHLCV values")
        if t % 60000 != 0:
            raise ValueError("Expected one-minute candles")
        if t + 60000 <= now_ms:
            rows.append((t, o, h, l, c, v))
    if len(rows) < 35:
        return {"action": "wait", "reason": "Warming up: need 35 completed one-minute candles"}
    if any(b[0] - a[0] != 60000 for a, b in zip(rows, rows[1:])):
        raise ValueError("Candles are duplicated, unordered, or missing")
    if now_ms - rows[-1][0] > 120000:
        raise ValueError("Candle feed is stale")
    close = [r[4] for r in rows]
    deltas = [b - a for a, b in zip(close, close[1:])]
    gain = sum(max(d, 0) for d in deltas[:14]) / 14
    loss = sum(max(-d, 0) for d in deltas[:14]) / 14
    for d in deltas[14:]:
        gain = (gain * 13 + max(d, 0)) / 14
        loss = (loss * 13 + max(-d, 0)) / 14
    rsi = 50 if gain == loss == 0 else (100 if loss == 0 else 100 - 100 / (1 + gain / loss))
    ranges = [max(r[2] - r[3], abs(r[2] - p[4]), abs(r[3] - p[4]))
              for p, r in zip(rows, rows[1:])]
    atr = sum(ranges[:14]) / 14
    for value in ranges[14:]:
        atr = (atr * 13 + value) / 14
    fast, slow = ema(close, 9), ema(close, 21)
    five, ten = (close[-1] / close[-6] - 1) * 100, (close[-1] / close[-11] - 1) * 100
    rising = sum(d > 0 for d in deltas[-10:]) / 10
    baseline_volume = sum(r[5] for r in rows[-21:-1]) / 20
    volume_ratio = rows[-1][5] / baseline_volume if baseline_volume else 0
    buy = fast > slow and five > 0 and ten > 0 and rising >= .7 and 50 <= rsi <= 78 and volume_ratio >= 1.1
    reverse = fast < slow or (five < 0 and rsi < 45)
    return {"action": "buy" if buy else "sell" if reverse else "wait",
            "reason": ("5/10-minute momentum, EMA, RSI and volume agree" if buy else
                       "Trend reversal" if reverse else "Waiting for momentum, volume and RSI confirmation"),
            "candle_time": int(rows[-1][0]), "price": close[-1], "ema9": fast,
            "ema21": slow, "rsi14": rsi, "atr14": atr, "momentum5_pct": five,
            "momentum10_pct": ten, "volume_ratio": volume_ratio}


def evaluate_position(position, bid, signal, now):
    high = max(position["high"], bid)
    position["high"] = high
    if bid <= position["stop"]:
        return "stop_loss"
    if bid >= position["target"]:
        return "take_profit"
    if high >= position["entry"] * (1 + position["stop_fraction"]) and bid <= high * (1 - position["stop_fraction"]):
        return "trailing_stop"
    if signal.get("action") == "sell":
        return "trend_reversal"
    if now - position["opened_at"] >= 1800:
        return "maximum_hold_time"
    return None


def validate_book(book, now):
    """Reject corruption rather than let the generic depth walker skip it."""
    for side in ("bids", "asks"):
        levels = book.get(side)
        if not isinstance(levels, list) or not levels:
            raise ValueError("Order book side is empty")
        previous = None
        for level in levels:
            if not isinstance(level, (list, tuple)) or len(level) < 2:
                raise ValueError("Malformed order book level")
            price, quantity = map(float, level[:2])
            if not all(math.isfinite(x) and x > 0 for x in (price, quantity)):
                raise ValueError("Order book values must be finite and positive")
            if previous is not None and ((side == "bids" and price > previous) or (side == "asks" and price < previous)):
                raise ValueError("Unordered order book")
            previous = price
    if float(book["bids"][0][0]) > float(book["asks"][0][0]):
        raise ValueError("Crossed order book")
    if book.get("timestamp") is not None:
        age = now * 1000 - float(book["timestamp"])
        if not math.isfinite(age) or not -1000 <= age <= 3000:
            raise ValueError("Stale or future order book")


def market_limit_error(market, quantity, cost):
    if market.get("active") is False or market.get("spot") is False:
        return "Market is not active spot trading"
    for field, value in (("amount", quantity), ("cost", cost)):
        limit = (market.get("limits") or {}).get(field) or {}
        for bound in ("min", "max"):
            if limit.get(bound) is None:
                continue
            threshold = D(limit[bound])
            if not threshold.is_finite() or threshold < 0:
                return "Invalid exchange market limits"
            if (bound == "min" and value < threshold) or (bound == "max" and value > threshold):
                return f"Order violates exchange {field} {bound}"
    return None


def paper_tick(account, client, exchange, symbols, quotes, cfg, now=None, cancelled=None):
    """Manage one paper position; candle entries once per minute, exits each scan."""
    if cfg.get("execution_mode") != "paper" or cfg.get("mode") != "live":
        raise ValueError("Signal strategy only supports live-data paper execution")
    now = time.time() if now is None else now
    tick_started = time.monotonic()
    session = account.signal_state
    marks = {s: q[exchange]["bid"] for s, q in quotes.items()
             if exchange in q and 0 < float(q[exchange].get("bid") or 0) < float("inf")}
    equity = account.total_value(marks)
    date = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    if session.get("day") != date:
        session.update(day=date, day_pnl=0.0, day_start_equity=equity)
    equity_loss = max(0, session.get("day_start_equity", equity) - equity)
    reports = {}
    position = session.get("position")
    watched = [position["symbol"]] if position else symbols
    for symbol in watched:
        if not symbol.endswith("/USDT"):
            continue
        quote = (quotes.get(symbol) or {}).get(exchange) or {}
        bid, ask = float(quote.get("bid") or 0), float(quote.get("ask") or 0)
        if not (0 < bid <= ask and math.isfinite(ask)):
            reports[symbol] = {"action": "wait", "reason": "No valid current bid/ask"}
            continue
        reason = evaluate_position(position, bid, {}, now) if position else None
        if position and equity_loss >= float(cfg["max_daily_loss"]):
            reason = "daily_equity_loss"
        report = {"action": "hold", "reason": reason or "Monitoring"}
        if not reason:
            try:
                cache = account.signal_cache.get(symbol)
                if not cache or now - cache[0] >= 15:
                    candles = client.fetch_ohlcv(symbol, timeframe="1m", limit=80)
                    cache = (now, candles)
                    account.signal_cache[symbol] = cache
                report = analyse(cache[1], (now + time.monotonic() - tick_started) * 1000)
            except Exception as exc:
                report = {"action": "wait", "reason": f"Candle data unavailable: {type(exc).__name__}"}
            if position:
                reason = evaluate_position(position, bid, report, now)
        reports[symbol] = report
        if position and not reason:
            report.update(action="hold", reason="Managing open paper position")
            continue
        if not position:
            if time.monotonic() - tick_started > 3:
                report.update(action="wait", reason="Quote reference aged during data fetch; retry next scan")
                continue
            if report["action"] != "buy":
                continue
            if now < session.get("cooldown_until", 0) or report.get("candle_time") == session.get("last_entry_candle"):
                report.update(action="wait", reason="Entry cooldown / candle already traded")
                continue
            if session.get("day_pnl", 0) <= -float(cfg["max_daily_loss"]) or equity_loss >= float(cfg["max_daily_loss"]):
                report.update(action="wait", reason="Daily loss cap reached; no new entries")
                continue
            entries = [t for t in session.get("entries", []) if now - t < 3600]
            if len(entries) >= int(cfg.get("max_trades_per_hour", 12)):
                report.update(action="wait", reason="Hourly trade limit reached")
                continue
        market = client.market(symbol)
        precision = (market.get("precision") or {}).get("amount")
        if precision is None or client.precisionMode not in (2, 4):
            raise ValueError("Exchange amount precision unavailable")
        step = D(precision) if client.precisionMode == 4 else D(1).scaleb(-int(precision))
        if not step.is_finite() or step <= 0:
            raise ValueError("Invalid exchange amount precision")
        fee = D(cfg["fee"])
        if not fee.is_finite() or not 0 <= fee < .05:
            raise ValueError("Invalid simulation fee")
        # A latency delay means the fill uses a newly fetched book, not the candle close.
        time.sleep(.25)
        started = time.monotonic()
        book = client.fetch_order_book(symbol, limit=50)
        if time.monotonic() - started > 3:
            report.update(action="wait", reason="Order book response too slow")
            continue
        validate_book(book, now + (time.monotonic() - tick_started))
        if cancelled and cancelled():
            report.update(action="wait", reason="Engine stopped before paper execution")
            break
        if position:
            quantity = D(position["quantity"])
            fill = books.fill_for_quantity(book.get("bids", []), quantity)
            rejection = books.rejection_reason("sell", fill, bid, cfg["max_slippage"], require_complete=True)
            rejection = rejection or market_limit_error(market, quantity, fill.notional)
            if rejection:
                report.update(action="hold", reason=f"Exit deferred: {rejection}")
                continue
            proceeds = fill.notional * (1 - fee)
            account.ledger.debit(exchange, symbol.split("/")[0], quantity)
            account.ledger.credit(exchange, "USDT", proceeds)
            profit = proceeds - D(position["cost"])
            account.profit = float(D(account.profit) + profit)
            session.update(position=None, cooldown_until=now + 300,
                           day_pnl=float(D(session.get("day_pnl", 0)) + profit))
            report.update(action="sell", reason=reason)
            return reports, {"symbol": symbol, "buy_exchange": exchange, "sell_exchange": exchange,
                "buy_price": position["entry"], "sell_price": float(fill.average_price),
                "trade_size_usdt": float(position["cost"]), "profit_usdt": float(profit),
                "net_profit_pct": float(profit / D(position["cost"]) * 100),
                "filled_quantity": float(quantity), "exit_reason": reason,
                "strategy": "signal_trend", "status": "filled", "execution_mode": "paper",
                "data_mode": "live", "performance_mode": "signal_paper",
                "time": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")}
        stop_fraction = min(.03, max(.005, 2 * report["atr14"] / ask))
        equity = account.total_value({symbol: bid})
        loss_remaining = max(0, float(cfg["max_daily_loss"]) - max(equity_loss, -session.get("day_pnl", 0)))
        budget = min(float(cfg["trade_size"]), float(cfg["max_position_notional"]),
                     float(account.ledger.get(exchange, "USDT")),
                     min(equity * .0025, loss_remaining) / (stop_fraction + 2 * float(fee)))
        minimum = max(10, float(((market.get("limits") or {}).get("cost") or {}).get("min") or 0))
        if budget < minimum:
            report.update(action="wait", reason="Risk-sized amount below exchange minimum")
            continue
        estimate = books.fill_for_notional(book.get("asks", []), D(budget) / (1 + fee))
        quantity = floor_to_step(estimate.quantity, step)
        fill = books.fill_for_quantity(book.get("asks", []), quantity)
        rejection = books.rejection_reason("buy", fill, ask, cfg["max_slippage"], require_complete=True)
        rejection = rejection or market_limit_error(market, quantity, fill.notional)
        cost = fill.notional * (1 + fee)
        if rejection or quantity <= 0 or fill.notional < D(minimum) or cost > D(budget):
            report.update(action="wait", reason=rejection or "Rounded order fails size/minimum checks")
            continue
        account.ledger.debit(exchange, "USDT", cost)
        account.ledger.credit(exchange, symbol.split("/")[0], quantity)
        entry = float(fill.average_price)
        session.update(position={"symbol": symbol, "exchange": exchange, "quantity": str(quantity),
            "cost": str(cost), "entry": entry, "high": entry, "opened_at": now,
            "stop_fraction": stop_fraction, "stop": entry * (1 - stop_fraction),
            "target": entry * (1 + 2 * stop_fraction + 2 * float(fee))},
            last_entry_candle=report["candle_time"], entries=entries + [now])
        report.update(action="buy", reason="Paper position opened with automatic stop/target")
        break  # One open position per account, never one per selected market.
    return reports, None
