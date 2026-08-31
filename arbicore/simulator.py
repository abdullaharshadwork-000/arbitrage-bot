"""Paper execution that can actually lose money.

The original paper path filled at top-of-book, instantly, with unlimited
depth, against per-symbol wallets that never ran dry. Every scan that found a
gap booked the full theoretical profit, which is why the demo database shows
$8,743 of "profit" that predicts nothing.

This module fills paper orders the way a real order fills:

  * against a real order book, walked level by level for a true VWAP
  * after a latency delay, using the book as it looks *then*, not at scan time
  * out of shared balances that deplete and must be rebalanced
  * with partial fills when the visible depth runs out

The output records both the profit the scan predicted and the profit the fill
produced, so the gap between them — slippage and latency cost — is measurable
rather than invisible.
"""

import time
from dataclasses import dataclass, field
from decimal import Decimal

from . import books
from .ledger import InsufficientBalance, Ledger, split_symbol
from .money import D, HUNDRED, ONE, ZERO, floor_to_step, net_spread_pct


@dataclass(frozen=True)
class SimulatedLeg:
    """One simulated fill, with the quote it was decided on for comparison."""

    exchange: str
    symbol: str
    side: str
    quoted_price: Decimal
    requested_quantity: Decimal
    filled_quantity: Decimal
    average_price: Decimal
    notional: Decimal
    fee_cost: Decimal
    fee_currency: str
    complete: bool
    latency_ms: int

    @property
    def slippage_pct(self):
        if self.quoted_price <= ZERO or self.average_price <= ZERO:
            return ZERO
        raw = (self.average_price - self.quoted_price) / self.quoted_price * HUNDRED
        return raw if self.side == "buy" else -raw

    def as_dict(self):
        return {
            "exchange": self.exchange, "symbol": self.symbol, "side": self.side,
            "quoted_price": float(self.quoted_price),
            "requested_quantity": float(self.requested_quantity),
            "filled_quantity": float(self.filled_quantity),
            "average_price": float(self.average_price),
            "notional": float(self.notional),
            "fee_cost": float(self.fee_cost), "fee_currency": self.fee_currency,
            "complete": self.complete, "latency_ms": self.latency_ms,
            "slippage_pct": float(self.slippage_pct),
        }


@dataclass(frozen=True)
class SimulatedTrade:
    """Result of one simulated arbitrage attempt, including the rejections."""

    ok: bool
    reason: str = ""
    legs: tuple = ()
    expected_profit: Decimal = ZERO   # what the scan believed
    realized_profit: Decimal = ZERO   # what the fills produced
    symbol: str = ""
    strategy: str = "cross_exchange"
    # Set when a multi-leg route died after committing capital. The position is
    # real, sitting in an intermediate coin, and someone has to unwind it.
    stranded_currency: str = ""
    stranded_quantity: Decimal = ZERO
    stranded_exchange: str = ""

    @property
    def slippage_cost(self):
        """Expected minus realized: the price of latency and depth."""
        return self.expected_profit - self.realized_profit

    @property
    def stranded(self):
        return self.stranded_quantity > ZERO

    def as_dict(self):
        return {
            "ok": self.ok, "reason": self.reason, "symbol": self.symbol,
            "strategy": self.strategy,
            "expected_profit": float(self.expected_profit),
            "realized_profit": float(self.realized_profit),
            "slippage_cost": float(self.slippage_cost),
            "stranded": self.stranded,
            "stranded_currency": self.stranded_currency,
            "stranded_quantity": float(self.stranded_quantity),
            "stranded_exchange": self.stranded_exchange,
            "legs": [leg.as_dict() for leg in self.legs],
        }


def synthetic_book(mid, side, depth_levels=8, top_size=None, step_pct="0.02",
                   size_growth="1.6"):
    """Build a plausible book around `mid` for offline simulation.

    Demo mode has no real book, but filling at a single price teaches the wrong
    lesson. This produces a thin top level with size growing as price worsens —
    the shape that makes naive slippage checks pass and real fills disappoint.
    """
    centre = D(mid)
    if centre <= ZERO:
        return []
    increment = centre * D(step_pct) / HUNDRED
    size = D(top_size) if top_size is not None else centre / D(2000)
    growth = D(size_growth)
    direction = ONE if side == "asks" else -ONE
    levels = []
    for index in range(int(depth_levels)):
        price = centre + direction * increment * D(index)
        if price <= ZERO:
            break
        levels.append([price, size])
        size *= growth
    return levels


def chain_route(route, start_currency):
    """Expand [(symbol, side), ...] into (symbol, side, spent, received) steps.

    Raises ValueError when the currencies do not actually chain, or when the
    loop does not return to `start_currency`. A route that does not close is
    not an arbitrage — it is an unhedged directional bet, and the old engine's
    route builder had no check that the last hop came home.
    """
    if not route:
        raise ValueError("route is empty")
    holding = str(start_currency)
    steps = []
    for entry in route:
        symbol, side = entry[0], str(entry[1]).lower()
        base, quote = split_symbol(symbol)
        if side == "buy":
            spent, received = quote, base
        elif side == "sell":
            spent, received = base, quote
        else:
            raise ValueError(f"unknown side {side!r} for {symbol}")
        if spent != holding:
            raise ValueError(
                f"route breaks at {symbol} {side}: holding {holding}, "
                f"but this leg spends {spent}")
        steps.append((symbol, side, spent, received))
        holding = received
    if holding != str(start_currency):
        raise ValueError(
            f"route does not close: ends holding {holding}, "
            f"started from {start_currency}")
    return steps


class FillSimulator:
    """Executes paper trades against real books, after a latency delay.

    `book_source(exchange, symbol, side)` returns ccxt-style levels for the
    side being consumed ("asks" when buying, "bids" when selling) and is
    called *after* the latency wait, so it naturally reflects a market that
    moved while the bot was deciding.
    """

    def __init__(self, ledger=None, book_source=None, fee_provider=None,
                 step_provider=None, latency_ms=250, quote_currency="USDT",
                 sleep=time.sleep):
        self.ledger = ledger if ledger is not None else Ledger()
        self.book_source = book_source or (lambda exchange, symbol, side: [])
        self.fee_provider = fee_provider or (lambda exchange, symbol: D("0.001"))
        self.step_provider = step_provider or (lambda exchange, symbol: None)
        self.latency_ms = int(latency_ms)
        self.quote_currency = quote_currency
        self._sleep = sleep

    def _wait(self):
        if self.latency_ms > 0:
            self._sleep(self.latency_ms / 1000.0)

    def _book(self, exchange, symbol, side):
        return self.book_source(exchange, symbol, side) or []

    def _fee(self, exchange, symbol):
        return D(self.fee_provider(exchange, symbol))

    def _step(self, exchange, symbol):
        return self.step_provider(exchange, symbol)

    @staticmethod
    def _reject(reason, symbol, expected, strategy="cross_exchange", legs=()):
        return SimulatedTrade(ok=False, reason=reason, symbol=symbol,
                              expected_profit=expected, legs=tuple(legs),
                              strategy=strategy)

    def execute_cross_exchange(self, buy_exchange, sell_exchange, symbol,
                               notional, quoted_buy, quoted_sell,
                               max_slippage_pct="0.25", min_profit_pct="0.15"):
        """Buy on one venue, sell the received coin on the other.

        Returns a SimulatedTrade that is `ok=False` with a reason whenever a
        real preflight would have refused — thin depth, adverse move, drained
        balance. Those rejections are the useful output: they are the trades
        the naive simulator counted as wins.
        """
        base, quote = split_symbol(symbol)
        budget = D(notional)
        buy_fee = self._fee(buy_exchange, symbol)
        sell_fee = self._fee(sell_exchange, symbol)
        expected = budget * net_spread_pct(quoted_buy, quoted_sell,
                                           buy_fee, sell_fee) / HUNDRED

        if not self.ledger.can_cover(buy_exchange, quote, budget):
            return self._reject(
                f"{buy_exchange} is out of {quote}: "
                f"{float(self.ledger.get(buy_exchange, quote)):.4f} left, "
                f"needs {float(budget):.4f} - rebalance required",
                symbol, expected)

        self._wait()

        asks = self._book(buy_exchange, symbol, "asks")
        entry = books.fill_for_notional(asks, budget)
        reason = books.rejection_reason("buy", entry, quoted_buy, max_slippage_pct,
                                        require_complete=False)
        if reason:
            return self._reject(f"buy leg rejected: {reason}", symbol, expected)

        step = self._step(buy_exchange, symbol)
        gross_base = floor_to_step(entry.quantity, step)
        if gross_base <= ZERO:
            return self._reject(
                "buy size rounds to zero at this exchange's step size",
                symbol, expected)
        spend = gross_base * entry.average_price
        net_base = floor_to_step(gross_base * (ONE - buy_fee),
                                 self._step(sell_exchange, symbol))
        if net_base <= ZERO:
            return self._reject("post-fee size rounds to zero", symbol, expected)

        if not self.ledger.can_cover(sell_exchange, base, net_base):
            return self._reject(
                f"{sell_exchange} is out of {base}: "
                f"{float(self.ledger.get(sell_exchange, base)):.8f} left, "
                f"needs {float(net_base):.8f} - rebalance required",
                symbol, expected)

        bids = self._book(sell_exchange, symbol, "bids")
        exit_fill = books.fill_for_quantity(bids, net_base)
        reason = books.rejection_reason("sell", exit_fill, quoted_sell,
                                        max_slippage_pct, require_complete=False)
        if reason:
            return self._reject(f"sell leg rejected: {reason}", symbol, expected)

        sold = floor_to_step(exit_fill.quantity, self._step(sell_exchange, symbol))
        if sold <= ZERO:
            return self._reject("sell size rounds to zero", symbol, expected)
        proceeds = sold * exit_fill.average_price
        sell_fee_cost = proceeds * sell_fee

        # Recheck the edge at the prices we would actually get. This is the
        # gate the old engine lacked: it committed on scan-time prices.
        realized_spread = net_spread_pct(entry.average_price,
                                         exit_fill.average_price,
                                         buy_fee, sell_fee)
        if realized_spread < D(min_profit_pct):
            return self._reject(
                f"edge collapsed to {float(realized_spread):.4f}% at the real "
                f"fill prices, below the {float(D(min_profit_pct)):.2f}% floor",
                symbol, expected)

        try:
            self.ledger.apply_fill(buy_exchange, symbol, "buy", gross_base,
                                   spend, gross_base * buy_fee, base)
            self.ledger.apply_fill(sell_exchange, symbol, "sell", sold,
                                   proceeds, sell_fee_cost, quote)
        except InsufficientBalance as exc:
            return self._reject(f"ledger refused the fill: {exc}", symbol, expected)

        legs = (
            SimulatedLeg(buy_exchange, symbol, "buy", D(quoted_buy), entry.quantity,
                         gross_base, entry.average_price, spend,
                         gross_base * buy_fee, base, entry.complete,
                         self.latency_ms),
            SimulatedLeg(sell_exchange, symbol, "sell", D(quoted_sell), net_base,
                         sold, exit_fill.average_price, proceeds,
                         sell_fee_cost, quote, exit_fill.complete, 0),
        )
        realized = (proceeds - sell_fee_cost) - spend
        return SimulatedTrade(ok=True, legs=legs, symbol=symbol,
                              expected_profit=expected, realized_profit=realized)

    def quoted_route_return(self, exchange, steps, notional, quoted_prices):
        """What the quoted top-of-book prices promise a route will return.

        This is the number the old engine treated as profit. Compare it to the
        realized figure from `execute_triangular` to see what three legs of
        depth and latency actually cost. Returns None if any leg is unpriced.
        """
        amount = D(notional)
        for symbol, side, _spent, _received in steps:
            price = D((quoted_prices or {}).get(symbol))
            if price <= ZERO:
                return None
            fee = self._fee(exchange, symbol)
            if side == "buy":
                amount = (amount / price) * (ONE - fee)
            else:
                amount = (amount * price) * (ONE - fee)
        return amount

    def execute_triangular(self, exchange, route, notional, quoted_prices,
                           max_slippage_pct="0.25", min_profit_pct="0.15",
                           start_currency=None):
        """Walk a closed loop of pairs on one venue, committed from leg one.

        `route` is a sequence of (symbol, side) steps that chain back to the
        starting currency, e.g.
        `[("BTC/USDT", "buy"), ("ETH/BTC", "buy"), ("ETH/USDT", "sell")]`.

        There is no post-fill abort here. Once the first leg fills the capital
        sits in an intermediate coin, and the only exits are finishing the loop
        or unwinding at a loss. So the profit floor is checked once, on quoted
        prices, and every leg after that is *recorded* rather than vetoed —
        including routes that end down. A leg that cannot fill leaves the trade
        `ok=False` with `stranded_*` populated: a real position, in a coin you
        did not want, that a human has to unwind.
        """
        start = str(start_currency or self.quote_currency)
        steps = chain_route(route, start)
        label = " -> ".join(f"{sym}:{side}" for sym, side, _s, _r in steps)
        budget = D(notional)
        expected = ZERO

        if budget <= ZERO:
            return self._reject("notional is zero", label, ZERO, "triangular")

        promised = self.quoted_route_return(exchange, steps, budget, quoted_prices)
        if promised is None:
            return self._reject(f"a leg of {label} has no quoted price",
                                label, ZERO, "triangular")
        expected = promised - budget
        expected_pct = (promised / budget - ONE) * HUNDRED
        if expected_pct < D(min_profit_pct):
            return self._reject(
                f"quoted route return {float(expected_pct):.4f}% is below the "
                f"{float(D(min_profit_pct)):.2f}% floor",
                label, expected, "triangular")

        if not self.ledger.can_cover(exchange, start, budget):
            return self._reject(
                f"{exchange} is out of {start}: "
                f"{float(self.ledger.get(exchange, start)):.8f} left, "
                f"needs {float(budget):.8f} - rebalance required",
                label, expected, "triangular")

        held = budget
        currency = start
        legs = []

        def fail(detail):
            # Before the first fill nothing is committed, so this is a clean
            # veto. After it, `held` is real inventory in the wrong currency.
            if not legs:
                return self._reject(detail, label, expected, "triangular")
            return SimulatedTrade(
                ok=False, reason=detail, legs=tuple(legs), symbol=label,
                strategy="triangular", expected_profit=expected,
                realized_profit=ZERO, stranded_currency=currency,
                stranded_quantity=held, stranded_exchange=exchange)

        for symbol, side, _spent, received in steps:
            # Latency on every hop, not just the first: a three-leg route is
            # three chances for the book to move away from you.
            self._wait()
            fee = self._fee(exchange, symbol)
            step = self._step(exchange, symbol)
            quoted = D((quoted_prices or {}).get(symbol))

            if side == "buy":
                fill = books.fill_for_notional(
                    self._book(exchange, symbol, "asks"), held)
            else:
                offered = floor_to_step(held, step)
                if offered <= ZERO:
                    return fail(f"{symbol} sell size rounds to zero at the "
                                f"exchange step size")
                fill = books.fill_for_quantity(
                    self._book(exchange, symbol, "bids"), offered)

            reason = books.rejection_reason(side, fill, quoted, max_slippage_pct,
                                            require_complete=False)
            if reason:
                return fail(f"{symbol} {side} leg rejected: {reason}")

            quantity = floor_to_step(fill.quantity, step)
            if quantity <= ZERO:
                return fail(f"{symbol} {side} fill rounds to zero at the "
                            f"exchange step size")
            traded = quantity * fill.average_price
            gross = quantity if side == "buy" else traded
            fee_cost = gross * fee

            try:
                self.ledger.apply_fill(exchange, symbol, side, quantity, traded,
                                       fee_cost, received)
            except InsufficientBalance as exc:
                return fail(f"ledger refused the {symbol} {side} leg: {exc}")

            legs.append(SimulatedLeg(
                exchange, symbol, side, quoted, fill.quantity, quantity,
                fill.average_price, traded, fee_cost, received, fill.complete,
                self.latency_ms))
            # Whatever the step size rounded off stays in the ledger and is
            # simply not carried forward, so realized profit understates by the
            # dust rather than inventing it.
            held = gross - fee_cost
            currency = received

        realized = held - budget
        return SimulatedTrade(ok=True, legs=tuple(legs), symbol=label,
                              strategy="triangular", expected_profit=expected,
                              realized_profit=realized)
