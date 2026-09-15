"""Realized closed-trade metrics. Never substitutes wallet valuation for P&L."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


def summarize(rows, now=None):
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(timezone.utc).date()
    total = daily = Decimal(0)
    count = wins = losses = breakeven = day_count = excluded = 0
    for stamp, raw_profit, status in rows:
        if str(status or "").lower() not in {"filled", "closed", "completed"}:
            excluded += 1
            continue
        try:
            profit = Decimal(str(raw_profit))
            moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            moment = moment.astimezone(timezone.utc)
            if not profit.is_finite() or moment > now:
                raise ValueError("Invalid trade evidence")
        except (ValueError, TypeError, InvalidOperation):
            excluded += 1
            continue
        total += profit
        count += 1
        wins += int(profit > 0)
        losses += int(profit < 0)
        breakeven += int(profit == 0)
        if moment.date() == today:
            daily += profit
            day_count += 1
    return {"total_profit": float(total), "today_profit": float(daily),
            "total_trades": count, "today_trades": day_count,
            "wins": wins, "losses": losses, "breakeven": breakeven,
            "win_rate": wins / count * 100 if count else 0.0,
            "excluded_trades": excluded, "day": today.isoformat(), "day_timezone": "UTC"}


def scope_sql(mode):
    # Only classify old rows when their paper/data provenance is explicit.
    # Unknown old real rows must never be guessed to be production or testnet.
    if mode in {"tutorial", "paper"}:
        return ("(performance_mode = ? OR (COALESCE(performance_mode,'legacy') = 'legacy' "
                "AND execution_mode = 'paper' AND data_mode = ?))", [mode, "demo" if mode == "tutorial" else "live"])
    return "performance_mode = ?", [mode]
