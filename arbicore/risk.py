"""Limits that stop the bot, not warnings that scroll past.

The engine had exactly one control: a human clicking Emergency Stop on a
dashboard they had to be watching. Everything that actually kills an arbitrage
account happens faster than that — a stale feed that reports a 3% spread on
every scan, a venue that starts rejecting sells while buys keep filling, a
partial fill leaving inventory nobody unwinds. Each one produces a *stream* of
losing trades, and the loop had no memory between them.

Every limit here is a hard stop with a stated reason, evaluated before an order
is placed. The design rule: a tripped limit halts the loop and requires an
explicit human resume. Automatic recovery from a condition you do not
understand yet is how a small loss becomes the whole balance.
"""

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from .money import D, ZERO

# Why the bot stopped. Recorded verbatim so the dashboard and the alert say the
# same thing, and so the DB row is greppable afterwards.
HALT_DAILY_LOSS = "daily_loss_limit"
HALT_CONSECUTIVE = "consecutive_failures"
HALT_DRAWDOWN = "equity_drawdown"
HALT_STRANDED = "stranded_position"
HALT_RECONCILE = "unreconciled_fill"
HALT_MANUAL = "manual"
HALT_KILL = "kill_switch"


def _utc_day(moment):
    return datetime.fromtimestamp(moment, tz=timezone.utc).date().isoformat()


@dataclass(frozen=True)
class RiskDecision:
    """Whether one specific trade may proceed."""

    allowed: bool
    reason: str = ""
    limit: str = ""

    def __bool__(self):
        return self.allowed


ALLOWED = RiskDecision(True)


class RiskManager:
    """Pre-trade gate and post-trade tally, with a latch that needs a human.

    `clock` is injectable so the daily rollover and the rate window can be
    tested without waiting a day. Nothing here touches the network; the caller
    decides what to do with a halt (log it, alert, stop the loop).
    """

    def __init__(self, settings, clock=time.time):
        self.settings = settings
        self._clock = clock
        self._day = _utc_day(clock())
        self.realized_today = ZERO
        self.trades_today = 0
        self.wins_today = 0
        self.losses_today = 0
        self.consecutive_failures = 0
        self.peak_equity = ZERO
        self.equity = ZERO
        self.halted = False
        self.halt_limit = ""
        self.halt_reason = ""
        self.halted_at = None
        self.killed = False
        self.stranded = []           # positions a human still has to unwind
        self._order_times = deque()  # for the per-minute cap
        self.history = deque(maxlen=200)   # recent halts/limit hits, newest last

    # ------------------------------------------------------------------ state

    def _roll_day(self):
        today = _utc_day(self._clock())
        if today != self._day:
            self._day = today
            self.realized_today = ZERO
            self.trades_today = 0
            self.wins_today = 0
            self.losses_today = 0
            # Deliberately NOT reset: consecutive_failures, halted, stranded.
            # A broken venue at 23:59 is still broken at 00:01.

    def _note(self, limit, reason):
        self.history.append({"at": self._clock(), "limit": limit, "reason": reason})

    def halt(self, limit, reason):
        """Latch the loop off. Only `resume()` clears it."""
        if not self.halted:
            self.halted = True
            self.halt_limit = limit
            self.halt_reason = reason
            self.halted_at = self._clock()
            self._note(limit, reason)
        return RiskDecision(False, reason, limit)

    def kill(self, reason="kill switch engaged"):
        """Permanent for this process. Survives `resume()`."""
        self.killed = True
        return self.halt(HALT_KILL, reason)

    def resume(self):
        """Clear a halt after a human has looked at it.

        Refuses while the kill switch is on or a stranded position is open,
        because "resume" would otherwise mean "keep trading around a position
        of unknown size", which is how one bad fill becomes several.
        """
        if self.killed:
            return False, "kill switch is engaged; restart the process deliberately"
        if self.stranded and self.settings.halt_on_stranded_position:
            return False, (f"{len(self.stranded)} stranded position(s) still open; "
                           f"unwind them first")
        self.halted = False
        self.halt_limit = ""
        self.halt_reason = ""
        self.halted_at = None
        self.consecutive_failures = 0
        return True, "resumed"

    # -------------------------------------------------------------- pre-trade

    def _recent_orders(self):
        cutoff = self._clock() - 60.0
        while self._order_times and self._order_times[0] < cutoff:
            self._order_times.popleft()
        return len(self._order_times)

    def check(self, notional, symbol=""):
        """May this trade be placed? Evaluated in order of severity."""
        self._roll_day()
        settings = self.settings
        where = f" ({symbol})" if symbol else ""

        if self.killed:
            return RiskDecision(False, "kill switch is engaged", HALT_KILL)
        if self.halted:
            return RiskDecision(False, f"halted: {self.halt_reason}",
                                self.halt_limit)

        if self.stranded and settings.halt_on_stranded_position:
            position = self.stranded[-1]
            return self.halt(
                HALT_STRANDED,
                f"{float(position['quantity'])} {position['currency']} is stranded "
                f"on {position['exchange']}; unwind it before trading again")

        loss = -self.realized_today
        if loss >= settings.max_daily_loss_usdt:
            return self.halt(
                HALT_DAILY_LOSS,
                f"down {float(loss):.2f} USDT today, at or past the "
                f"{float(settings.max_daily_loss_usdt):.2f} daily limit")

        if self.consecutive_failures >= settings.max_consecutive_failures:
            return self.halt(
                HALT_CONSECUTIVE,
                f"{self.consecutive_failures} trades in a row failed; something "
                f"is wrong with the venue or the feed, not with the spread")

        drawdown = self.peak_equity - self.equity
        if self.peak_equity > ZERO and drawdown >= settings.max_daily_loss_usdt:
            return self.halt(
                HALT_DRAWDOWN,
                f"equity is {float(drawdown):.2f} USDT below its peak of "
                f"{float(self.peak_equity):.2f}")

        size = D(notional)
        if size <= ZERO:
            return RiskDecision(False, f"trade size is zero{where}", "size")
        if size > settings.max_position_notional_usdt:
            return RiskDecision(
                False,
                f"{float(size):.2f} USDT{where} exceeds the "
                f"{float(settings.max_position_notional_usdt):.2f} per-position cap",
                "position_notional")
        if size < settings.min_notional_usdt:
            return RiskDecision(
                False,
                f"{float(size):.2f} USDT{where} is below the "
                f"{float(settings.min_notional_usdt):.2f} exchange minimum",
                "min_notional")

        placed = self._recent_orders()
        if placed >= settings.max_orders_per_minute:
            return RiskDecision(
                False,
                f"{placed} orders in the last minute, at the "
                f"{settings.max_orders_per_minute}/min cap",
                "order_rate")
        return ALLOWED

    # ------------------------------------------------------------- post-trade

    def record_order(self):
        """Call once per order actually sent, for the rate window."""
        self._order_times.append(self._clock())

    def record_success(self, realized_profit):
        """A completed round trip. Profit may still be negative."""
        self._roll_day()
        profit = D(realized_profit)
        self.realized_today += profit
        self.trades_today += 1
        if profit >= ZERO:
            self.wins_today += 1
            # Only a profitable trade clears the failure streak. A string of
            # small losses is still the venue telling you something.
            self.consecutive_failures = 0
        else:
            self.losses_today += 1
            self.consecutive_failures += 1
        return self.check_after()

    def record_rejection(self, reason=""):
        """A pre-trade veto. Not a failure — nothing was risked."""
        self._roll_day()
        return ALLOWED

    def record_failure(self, reason=""):
        """An attempt that broke after committing: a raised order error."""
        self._roll_day()
        self.consecutive_failures += 1
        self._note("failure", reason or "trade failed")
        return self.check_after()

    def record_stranded(self, exchange, currency, quantity, detail=""):
        """A position exists that the strategy did not intend to hold."""
        entry = {"at": self._clock(), "exchange": str(exchange),
                 "currency": str(currency), "quantity": D(quantity),
                 "detail": detail}
        self.stranded.append(entry)
        self._note(HALT_STRANDED, detail or
                   f"{float(entry['quantity'])} {entry['currency']} stranded "
                   f"on {entry['exchange']}")
        if self.settings.halt_on_stranded_position:
            self.halt(HALT_STRANDED, self.history[-1]["reason"])
        return entry

    def clear_stranded(self, index=None):
        """Mark stranded inventory as unwound, after a human says it is."""
        if index is None:
            self.stranded.clear()
        elif 0 <= index < len(self.stranded):
            self.stranded.pop(index)
        return len(self.stranded)

    def update_equity(self, value):
        """Feed total portfolio value in quote terms; tracks the peak."""
        self.equity = D(value)
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity
        return self.equity

    def check_after(self):
        """Re-evaluate the latching limits once a trade has been booked."""
        settings = self.settings
        loss = -self.realized_today
        if loss >= settings.max_daily_loss_usdt:
            return self.halt(
                HALT_DAILY_LOSS,
                f"down {float(loss):.2f} USDT today, at or past the "
                f"{float(settings.max_daily_loss_usdt):.2f} daily limit")
        if self.consecutive_failures >= settings.max_consecutive_failures:
            return self.halt(
                HALT_CONSECUTIVE,
                f"{self.consecutive_failures} trades in a row failed; something "
                f"is wrong with the venue or the feed, not with the spread")
        return ALLOWED

    # -------------------------------------------------------------- rendering

    def snapshot(self):
        """JSON-safe status for the dashboard and the alert body."""
        self._roll_day()
        return {
            "day": self._day,
            "halted": self.halted,
            "halt_limit": self.halt_limit,
            "halt_reason": self.halt_reason,
            "halted_at": self.halted_at,
            "killed": self.killed,
            "realized_today": float(self.realized_today),
            "trades_today": self.trades_today,
            "wins_today": self.wins_today,
            "losses_today": self.losses_today,
            "consecutive_failures": self.consecutive_failures,
            "max_consecutive_failures": self.settings.max_consecutive_failures,
            "daily_loss_limit": float(self.settings.max_daily_loss_usdt),
            "daily_loss_used_pct": self._loss_used_pct(),
            "equity": float(self.equity),
            "peak_equity": float(self.peak_equity),
            "drawdown": float(self.peak_equity - self.equity),
            "orders_last_minute": self._recent_orders(),
            "order_rate_limit": self.settings.max_orders_per_minute,
            "stranded": [
                {"at": item["at"], "exchange": item["exchange"],
                 "currency": item["currency"],
                 "quantity": float(item["quantity"]),
                 "detail": item["detail"]}
                for item in self.stranded
            ],
            "recent_limits": list(self.history)[-10:],
        }

    def _loss_used_pct(self):
        budget = self.settings.max_daily_loss_usdt
        if budget <= ZERO:
            return 0.0
        loss = -self.realized_today
        if loss <= ZERO:
            return 0.0
        return float(min(loss / budget, Decimal("1")) * 100)
