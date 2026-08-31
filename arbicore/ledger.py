"""Shared balances, one pot per (exchange, currency).

The old PaperWallet gave every *symbol* its own `cash/2` on every exchange:
five symbols across four venues meant twenty independent wallets that never
competed for capital and never ran dry. A real account has one USDT balance
per venue that every pair draws from, and cross-exchange arbitrage steadily
converts one side into the other — you end up long coin on the cheap venue
and long USDT on the expensive one until you rebalance.

Modelling that is the difference between a simulation that always wins and
one that tells you when you would have run out of sellable inventory.
"""

from collections import defaultdict
from decimal import Decimal

from .money import D, ZERO


class InsufficientBalance(RuntimeError):
    """A debit exceeded the available balance. Raised before any mutation."""

    def __init__(self, exchange, currency, requested, available):
        self.exchange = exchange
        self.currency = currency
        self.requested = D(requested)
        self.available = D(available)
        super().__init__(
            f"{exchange} has {float(self.available):.8f} {currency} available, "
            f"needs {float(self.requested):.8f}"
        )


def split_symbol(symbol):
    """'ETH/BTC' -> ('ETH', 'BTC'). Base is what you hold, quote is what you pay with."""
    base, _, quote = str(symbol).partition("/")
    return base, quote or "USDT"


class Ledger:
    """Balances keyed by exchange then currency, shared across every symbol."""

    def __init__(self, initial=None):
        self._balances = defaultdict(lambda: defaultdict(lambda: ZERO))
        for exchange, currencies in (initial or {}).items():
            for currency, amount in currencies.items():
                self._balances[exchange][currency] = D(amount)

    @classmethod
    def funded(cls, exchanges, holdings):
        """Give every exchange the same opening holdings, e.g. {'USDT': 500}."""
        return cls({ex: dict(holdings) for ex in exchanges})

    def exchanges(self):
        return sorted(self._balances)

    def currencies(self, exchange=None):
        if exchange is not None:
            return sorted(self._balances[exchange])
        found = set()
        for currencies in self._balances.values():
            found.update(currencies)
        return sorted(found)

    def get(self, exchange, currency):
        return self._balances[exchange][currency]

    def set_balance(self, exchange, currency, amount):
        self._balances[exchange][currency] = D(amount)

    def credit(self, exchange, currency, amount):
        value = D(amount)
        if value < ZERO:
            raise ValueError("credit expects a non-negative amount")
        self._balances[exchange][currency] += value
        return self._balances[exchange][currency]

    def debit(self, exchange, currency, amount):
        """Remove funds, or raise before touching anything.

        Checking and mutating in one place is what keeps a rejected leg from
        leaving the ledger half-updated.
        """
        value = D(amount)
        if value < ZERO:
            raise ValueError("debit expects a non-negative amount")
        available = self._balances[exchange][currency]
        if value > available:
            raise InsufficientBalance(exchange, currency, value, available)
        self._balances[exchange][currency] = available - value
        return self._balances[exchange][currency]

    def can_cover(self, exchange, currency, amount):
        return self._balances[exchange][currency] >= D(amount)

    def apply_fill(self, exchange, symbol, side, filled_quantity, cost,
                   fee_cost=ZERO, fee_currency=""):
        """Move balances to match an executed fill.

        `cost` is the quote actually exchanged and `fee_cost` the fee the
        exchange actually charged, in whichever currency it charged it. Fees
        are deducted from the currency they were levied in rather than assumed
        to be quote-denominated — BNB/KCS fee discounts bill in the native
        token, and pretending otherwise silently overstates profit.
        """
        base, quote = split_symbol(symbol)
        quantity = D(filled_quantity)
        notional = D(cost)

        if side == "buy":
            self.debit(exchange, quote, notional)
            self.credit(exchange, base, quantity)
        elif side == "sell":
            self.debit(exchange, base, quantity)
            self.credit(exchange, quote, notional)
        else:
            raise ValueError(f"unknown side: {side}")

        fee = D(fee_cost)
        if fee > ZERO and fee_currency:
            self.debit(exchange, fee_currency, fee)
        return self.snapshot(exchange)

    def snapshot(self, exchange=None):
        """Plain float dict for JSON/state payloads, zero balances omitted."""
        if exchange is not None:
            return {currency: float(amount)
                    for currency, amount in sorted(self._balances[exchange].items())
                    if amount != ZERO}
        return {ex: self.snapshot(ex) for ex in self.exchanges()}

    def value_in_quote(self, prices, quote="USDT"):
        """Total portfolio value, using {currency: price_in_quote} for conversion.

        A currency with no price is skipped rather than valued at zero, so a
        missing feed shows up as a smaller total instead of a phantom loss.
        """
        total = ZERO
        priced = {str(k): D(v) for k, v in (prices or {}).items()}
        for currencies in self._balances.values():
            for currency, amount in currencies.items():
                if amount == ZERO:
                    continue
                if currency == quote:
                    total += amount
                elif currency in priced and priced[currency] > ZERO:
                    total += amount * priced[currency]
        return total

    def inventory_skew(self, symbol, prices, quote="USDT"):
        """Per-exchange base-vs-quote split for one symbol, as a 0..1 ratio.

        1.0 means everything on that venue sits in the coin (nothing left to
        buy with), 0.0 means it is all quote (nothing left to sell). Arbitrage
        pushes venues toward the extremes; this is the number a rebalance
        policy watches.
        """
        base, _ = split_symbol(symbol)
        price = D((prices or {}).get(base))
        skew = {}
        for exchange in self.exchanges():
            base_value = self.get(exchange, base) * price
            quote_value = self.get(exchange, quote)
            total = base_value + quote_value
            skew[exchange] = float(base_value / total) if total > ZERO else 0.0
        return skew
