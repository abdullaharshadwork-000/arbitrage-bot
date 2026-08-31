"""Order-book analysis: what a given order would *actually* fill at.

The original engine compared the single best price against a reference and
summed the top ten levels to prove "enough liquidity". Both checks pass on a
book with a dust-sized top level and the real depth 2% away, which is exactly
the book shape that turns a projected profit into a realized loss.

Everything here walks the book level by level and reports the size-weighted
average price for the specific quantity being traded.
"""

from dataclasses import dataclass
from decimal import Decimal

from .money import D, HUNDRED, ONE, ZERO, pct_change


@dataclass(frozen=True)
class FillEstimate:
    """What walking the book predicts for one order."""

    quantity: Decimal          # base units the book can supply
    notional: Decimal          # quote units exchanged for that quantity
    average_price: Decimal     # notional / quantity — the price that matters
    best_price: Decimal        # top of book, the price the naive check used
    levels_consumed: int
    complete: bool             # False when the book ran out before the request

    @property
    def slippage_pct(self):
        """How far the average fill sits from the top of book, always >= 0."""
        if self.best_price <= ZERO or self.quantity <= ZERO:
            return ZERO
        return abs(pct_change(self.best_price, self.average_price))

    def as_dict(self):
        return {
            "quantity": float(self.quantity),
            "notional": float(self.notional),
            "average_price": float(self.average_price),
            "best_price": float(self.best_price),
            "levels_consumed": self.levels_consumed,
            "complete": self.complete,
            "slippage_pct": float(self.slippage_pct),
        }


EMPTY_FILL = FillEstimate(ZERO, ZERO, ZERO, ZERO, 0, False)


def _clean_levels(levels):
    """Drop malformed or non-positive entries; keep the exchange's ordering.

    ccxt gives asks ascending and bids descending, which is the order a market
    order consumes them in, so we never re-sort — re-sorting would silently
    hide a book that arrived corrupted.
    """
    cleaned = []
    for level in levels or []:
        try:
            price = D(level[0])
            amount = D(level[1])
        except (IndexError, TypeError):
            continue
        if price > ZERO and amount > ZERO:
            cleaned.append((price, amount))
    return cleaned


def fill_for_quantity(levels, quantity):
    """Walk the book to buy/sell `quantity` base units.

    `complete` is False when the visible book cannot supply the full quantity;
    the caller decides whether a partial estimate is acceptable. The returned
    average_price is the VWAP of what *was* available.
    """
    wanted = D(quantity)
    book = _clean_levels(levels)
    if wanted <= ZERO or not book:
        return EMPTY_FILL

    remaining = wanted
    notional = ZERO
    filled = ZERO
    consumed = 0
    for price, available in book:
        consumed += 1
        take = available if available < remaining else remaining
        notional += take * price
        filled += take
        remaining -= take
        if remaining <= ZERO:
            break

    if filled <= ZERO:
        return EMPTY_FILL
    return FillEstimate(
        quantity=filled,
        notional=notional,
        average_price=notional / filled,
        best_price=book[0][0],
        levels_consumed=consumed,
        complete=remaining <= ZERO,
    )


def fill_for_notional(levels, notional):
    """Walk the ask side spending up to `notional` quote units.

    Used for the entry leg, which is sized in USDT rather than in coin. The
    final level is consumed partially so the spend never exceeds the budget.
    """
    budget = D(notional)
    book = _clean_levels(levels)
    if budget <= ZERO or not book:
        return EMPTY_FILL

    remaining = budget
    spent = ZERO
    filled = ZERO
    consumed = 0
    for price, available in book:
        consumed += 1
        level_cost = available * price
        if level_cost <= remaining:
            take = available
            cost = level_cost
        else:
            cost = remaining
            take = remaining / price
        spent += cost
        filled += take
        remaining -= cost
        if remaining <= ZERO:
            break

    if filled <= ZERO:
        return EMPTY_FILL
    return FillEstimate(
        quantity=filled,
        notional=spent,
        average_price=spent / filled,
        best_price=book[0][0],
        levels_consumed=consumed,
        complete=remaining <= ZERO,
    )


def visible_notional(levels):
    """Total quote value resting in the visible book."""
    return sum((price * amount for price, amount in _clean_levels(levels)), ZERO)


def rejection_reason(side, estimate, reference_price, max_slippage_pct,
                     require_complete=True):
    """Return why this fill is unacceptable, or None when it passes.

    The tolerance is applied to `average_price`, not to the best price: an
    order that starts at the quoted price and finishes 3% away has slipped 3%,
    however good the first level looked.
    """
    if estimate is None or estimate.quantity <= ZERO:
        return f"no {side} liquidity in the visible book"
    if require_complete and not estimate.complete:
        return (f"visible {side} depth covers only "
                f"{float(estimate.quantity):.8f} units of the requested size")

    reference = D(reference_price)
    if reference <= ZERO:
        return "reference price is missing"
    tolerance = D(max_slippage_pct) / HUNDRED
    average = estimate.average_price
    # Deviation from the *reference*, not from this book's own top level:
    # a book that is uniformly 1% worse than the quote has zero internal
    # slippage and is still 1% of loss.
    drift = abs(pct_change(reference, average))

    if side == "buy" and average > reference * (ONE + tolerance):
        return (f"average buy fill {float(average):.8f} is "
                f"{float(drift):.3f}% above the quote, "
                f"past the {float(D(max_slippage_pct)):.2f}% limit")
    if side == "sell" and average < reference * (ONE - tolerance):
        return (f"average sell fill {float(average):.8f} is "
                f"{float(drift):.3f}% below the quote, "
                f"past the {float(D(max_slippage_pct)):.2f}% limit")
    return None
