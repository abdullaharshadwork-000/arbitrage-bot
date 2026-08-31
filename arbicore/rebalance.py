"""Rebalancing: the cost the arbitrage P&L never mentioned.

Cross-exchange arbitrage is directional in inventory even when it is neutral in
price. Every fill buys base on the cheap venue and sells it on the expensive
one, so the cheap venue accumulates coin and runs out of quote while the
expensive venue does the reverse. After enough trades there is nothing left to
trade with, and the only way back is to move funds between venues.

That move is not free and it is not instant:

  * a flat withdrawal fee, charged in the coin, independent of size
  * a network confirmation delay measured in minutes
  * the funds are unavailable to *both* venues while in flight
  * a minimum withdrawal amount that makes small rebalances impossible

The engine modelled none of this — its per-symbol wallets never ran dry, so it
never needed to. A strategy whose edge is 0.15% per trade and whose rebalance
costs the equivalent of several trades is not obviously profitable, and this
module is what makes that arithmetic visible instead of assumed.
"""

import time
from dataclasses import dataclass, field
from decimal import Decimal

from .ledger import InsufficientBalance, split_symbol
from .money import D, HUNDRED, ZERO

# (withdrawal fee in the coin, typical confirmation minutes, minimum withdrawal).
# Representative mainnet figures, not a live feed: the point is that the cost is
# non-zero and size-independent, which is what changes the strategy's economics.
NETWORK_DEFAULTS = {
    "BTC":  (Decimal("0.0002"), 30, Decimal("0.001")),
    "ETH":  (Decimal("0.0015"), 6,  Decimal("0.01")),
    "SOL":  (Decimal("0.01"),   1,  Decimal("0.05")),
    "XRP":  (Decimal("0.2"),    1,  Decimal("10")),
    "USDT": (Decimal("1"),      6,  Decimal("20")),   # ERC-20; TRC-20 is cheaper
    "USDC": (Decimal("1"),      6,  Decimal("20")),
}
FALLBACK_NETWORK = (Decimal("0.5"), 10, Decimal("5"))


def network_for(currency, overrides=None):
    """(fee, minutes, minimum) for a currency, overridable per deployment."""
    table = dict(NETWORK_DEFAULTS)
    table.update(overrides or {})
    fee, minutes, minimum = table.get(str(currency), FALLBACK_NETWORK)
    return D(fee), int(minutes), D(minimum)


@dataclass(frozen=True)
class Transfer:
    """One withdrawal in flight, or one that was refused."""

    source: str
    destination: str
    currency: str
    quantity: Decimal          # debited from the source
    fee: Decimal               # deducted on arrival
    minutes: int
    started_at: float = 0.0
    ok: bool = True
    reason: str = ""

    @property
    def arrives_at(self):
        return self.started_at + self.minutes * 60

    @property
    def credited(self):
        """What lands at the destination: size minus the network fee."""
        net = self.quantity - self.fee
        return net if net > ZERO else ZERO

    def as_dict(self):
        return {
            "source": self.source, "destination": self.destination,
            "currency": self.currency, "quantity": float(self.quantity),
            "fee": float(self.fee), "credited": float(self.credited),
            "minutes": self.minutes, "started_at": self.started_at,
            "arrives_at": self.arrives_at, "ok": self.ok, "reason": self.reason,
        }


def skew_targets(ledger, symbol, prices, quote="USDT"):
    """Per-exchange base share of that venue's capital for one symbol.

    Wraps `Ledger.inventory_skew` and adds the two things a planner needs: the
    venue that is furthest into coin and the one furthest into quote. Those are
    the two ends of the transfer.
    """
    skew = ledger.inventory_skew(symbol, prices, quote=quote)
    if not skew:
        return skew, None, None
    coin_heavy = max(skew, key=lambda name: skew[name])
    quote_heavy = min(skew, key=lambda name: skew[name])
    return skew, coin_heavy, quote_heavy


class RebalanceSimulator:
    """Moves inventory between venues, charging what a real transfer charges.

    Funds are debited when the transfer starts and credited only after the
    confirmation delay, so `in_flight` capital is genuinely unusable in between
    — which is the part that makes rebalancing expensive in a way fees alone do
    not capture.
    """

    def __init__(self, ledger, networks=None, clock=time.time, quote="USDT"):
        self.ledger = ledger
        self.networks = dict(networks or {})
        self._clock = clock
        self.quote = quote
        self.pending = []       # transfers not yet arrived
        self.completed = []
        self.refused = []
        self.fees_paid = {}     # currency -> total network fees

    # ------------------------------------------------------------- mechanics

    def start(self, source, destination, currency, quantity):
        """Debit the source now; the destination is credited on `settle()`."""
        fee, minutes, minimum = network_for(currency, self.networks)
        amount = D(quantity)
        now = self._clock()

        if source == destination:
            return self._refuse(source, destination, currency, amount, fee,
                                minutes, "source and destination are the same venue")
        if amount <= ZERO:
            return self._refuse(source, destination, currency, amount, fee,
                                minutes, "nothing to transfer")
        if amount < minimum:
            return self._refuse(
                source, destination, currency, amount, fee, minutes,
                f"{float(amount)} {currency} is below the {float(minimum)} network "
                f"minimum - this imbalance cannot be fixed by a transfer")
        if amount <= fee:
            return self._refuse(
                source, destination, currency, amount, fee, minutes,
                f"the {float(fee)} {currency} withdrawal fee would consume the "
                f"whole {float(amount)} transfer")
        try:
            self.ledger.debit(source, currency, amount)
        except InsufficientBalance as exc:
            return self._refuse(source, destination, currency, amount, fee,
                                minutes, str(exc))

        transfer = Transfer(source, destination, currency, amount, fee, minutes,
                            started_at=now)
        self.pending.append(transfer)
        return transfer

    def _refuse(self, source, destination, currency, amount, fee, minutes, reason):
        transfer = Transfer(source, destination, currency, D(amount), fee, minutes,
                            started_at=self._clock(), ok=False, reason=reason)
        self.refused.append(transfer)
        return transfer

    def settle(self, now=None):
        """Credit every transfer whose confirmation window has elapsed."""
        moment = self._clock() if now is None else float(now)
        arrived = [t for t in self.pending if t.arrives_at <= moment]
        self.pending = [t for t in self.pending if t.arrives_at > moment]
        for transfer in arrived:
            self.ledger.credit(transfer.destination, transfer.currency,
                               transfer.credited)
            self.fees_paid[transfer.currency] = (
                self.fees_paid.get(transfer.currency, ZERO) + transfer.fee)
            self.completed.append(transfer)
        return arrived

    # -------------------------------------------------------------- planning

    def plan(self, symbol, prices, trigger="0.85", trade_size="200",
             quote=None):
        """What to move to get the venues trading again, or why nothing helps.

        Only fires past `trigger` skew, because a transfer costs a fixed fee and
        several minutes: rebalancing on every small imbalance pays the fee more
        often than the strategy earns it.
        """
        quote_currency = quote or self.quote
        base, _ = split_symbol(symbol)
        threshold = D(trigger)
        size = D(trade_size)
        skew, coin_heavy, quote_heavy = skew_targets(
            self.ledger, symbol, prices, quote=quote_currency)
        if coin_heavy is None:
            return {"needed": False, "reason": "no balances to rebalance",
                    "skew": skew, "transfers": []}

        price = D((prices or {}).get(base))
        transfers = []
        notes = []

        # A venue past the trigger is holding coin it cannot buy more with; send
        # the surplus to whichever venue has run out of coin to sell.
        if skew[coin_heavy] >= threshold and price > ZERO:
            surplus_quote = (self.ledger.get(coin_heavy, base) * price
                             - size)   # keep one trade's worth in place
            if surplus_quote > ZERO:
                transfers.append({
                    "source": coin_heavy, "destination": quote_heavy,
                    "currency": base,
                    "quantity": surplus_quote / price,
                    "why": (f"{coin_heavy} is {float(skew[coin_heavy]) * 100:.1f}% "
                            f"in {base} and cannot buy; {quote_heavy} has none "
                            f"left to sell")})
        elif skew[coin_heavy] < threshold:
            notes.append(
                f"worst skew is {float(skew[coin_heavy]) * 100:.1f}%, under the "
                f"{float(threshold) * 100:.0f}% trigger")

        # The mirror case: a venue with only quote needs coin, or the loop stops
        # finding sellable inventory there.
        if skew[quote_heavy] <= (1 - threshold) and quote_heavy != coin_heavy:
            spare = self.ledger.get(quote_heavy, quote_currency) - size
            if spare > ZERO and not transfers:
                transfers.append({
                    "source": quote_heavy, "destination": coin_heavy,
                    "currency": quote_currency, "quantity": spare,
                    "why": (f"{quote_heavy} holds only {quote_currency}; moving "
                            f"some to {coin_heavy} lets it buy again")})

        costed = [self.cost_of(item, prices) for item in transfers]
        return {
            "needed": bool(transfers),
            "reason": "; ".join(notes) if notes and not transfers else "",
            "skew": skew,
            "transfers": costed,
            "total_cost_quote": float(sum(
                (D(item["cost_quote"]) for item in costed
                 if item["cost_quote"] is not None), ZERO)),
        }

    def cost_of(self, item, prices=None):
        """Attach the network's price to a planned transfer."""
        currency = item["currency"]
        fee, minutes, minimum = network_for(currency, self.networks)
        rate = (D(1) if currency == self.quote
                else D((prices or {}).get(currency), default=None))
        cost = fee * rate if rate and rate > ZERO else None
        detail = dict(item)
        detail.update({
            "quantity": float(D(item["quantity"])),
            "fee": float(fee),
            "minutes": minutes,
            "minimum": float(minimum),
            "cost_quote": float(cost) if cost is not None else None,
            "below_minimum": D(item["quantity"]) < minimum,
        })
        return detail

    # ------------------------------------------------------------- reporting

    def in_flight(self, prices=None):
        """Capital that belongs to neither venue right now, valued in quote."""
        total = ZERO
        for transfer in self.pending:
            rate = (D(1) if transfer.currency == self.quote
                    else D((prices or {}).get(transfer.currency), default=None))
            if rate and rate > ZERO:
                total += transfer.quantity * rate
        return total

    def total_fees_quote(self, prices=None):
        total = ZERO
        for currency, amount in self.fees_paid.items():
            rate = (D(1) if currency == self.quote
                    else D((prices or {}).get(currency), default=None))
            if rate and rate > ZERO:
                total += amount * rate
        return total

    def snapshot(self, prices=None):
        return {
            "pending": [t.as_dict() for t in self.pending],
            "completed": len(self.completed),
            "refused": [t.as_dict() for t in self.refused[-5:]],
            "in_flight_quote": float(self.in_flight(prices)),
            "fees_paid": {currency: float(amount)
                          for currency, amount in sorted(self.fees_paid.items())},
            "fees_paid_quote": float(self.total_fees_quote(prices)),
        }


def trades_to_repay(cost_quote, trade_size, net_profit_pct):
    """How many winning trades one rebalance costs.

    The number that decides whether the strategy is viable at all. A 200 USDT
    trade at a 0.15% net edge earns 0.30 USDT; a 20 USDT ERC-20 USDT withdrawal
    fee is therefore about 67 winning trades — which is the whole argument for
    cheap networks, exchange-internal transfers, or simply pre-funding both
    venues and never transferring.
    """
    cost = D(cost_quote)
    per_trade = D(trade_size) * D(net_profit_pct) / HUNDRED
    if per_trade <= ZERO:
        return None
    return cost / per_trade
