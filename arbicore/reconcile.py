"""Startup reconciliation: what the exchange knows that the bot forgot.

A crash, a killed terminal, or a Windows reboot at the wrong moment leaves the
bot's beliefs and the exchange's records disagreeing. The old engine started
clean every time — it rebuilt paper wallets from a constant and, in real mode,
would have begun placing new orders next to whatever the previous run left
behind. That is the specific failure that turns one stuck leg into a
compounding position.

Nothing here fixes anything automatically. It answers "what is out there that I
did not expect?" and refuses to declare the account safe until a human has
dealt with the answer. Automatic cleanup is available but must be asked for
explicitly, because cancelling an order or market-selling inventory is a
decision with a cost, not a housekeeping step.
"""

import time
from dataclasses import dataclass, field
from decimal import Decimal

from .money import D, ZERO, pct_change

# How far a balance may drift from the bot's expectation before it is reported.
# Not zero: fees, dust, and other activity on the same account all move it.
DEFAULT_BALANCE_TOLERANCE_PCT = Decimal("1.0")
DEFAULT_DUST_USDT = Decimal("1.0")


@dataclass(frozen=True)
class Discrepancy:
    """One thing that does not match, in words an operator can act on."""

    kind: str          # open_order | balance_drift | unresolved_record | clock_skew
    exchange: str
    detail: str
    blocking: bool = True
    data: dict = field(default_factory=dict)

    def as_dict(self):
        return {"kind": self.kind, "exchange": self.exchange,
                "detail": self.detail, "blocking": self.blocking,
                "data": self.data}


@dataclass(frozen=True)
class RecoveryReport:
    """The full answer to "is it safe to start?"."""

    checked_at: float
    exchanges: tuple = ()
    discrepancies: tuple = ()
    errors: tuple = ()      # venues that could not be queried at all

    @property
    def blocking(self):
        """Errors block too: you cannot call an account safe if you could not
        read it. A venue that refuses fetchOpenOrders may be hiding exactly the
        resting order this check exists to find."""
        return any(item.blocking for item in self.discrepancies) or bool(self.errors)

    @property
    def clean(self):
        return not self.discrepancies and not self.errors

    def of_kind(self, kind):
        return tuple(item for item in self.discrepancies if item.kind == kind)

    def summary(self):
        if self.clean:
            return "nothing outstanding: no open orders, balances as expected"
        parts = []
        for kind in ("open_order", "unresolved_record", "balance_drift",
                     "clock_skew"):
            found = self.of_kind(kind)
            if found:
                parts.append(f"{len(found)} {kind.replace('_', ' ')}(s)")
        if self.errors:
            parts.append(f"{len(self.errors)} venue(s) unreachable")
        return "; ".join(parts)

    def as_dict(self):
        return {
            "checked_at": self.checked_at,
            "exchanges": list(self.exchanges),
            "blocking": self.blocking,
            "clean": self.clean,
            "summary": self.summary(),
            "discrepancies": [item.as_dict() for item in self.discrepancies],
            "errors": list(self.errors),
        }


def open_orders_for(client, symbols):
    """Every resting order across the symbols we trade.

    Asks per symbol rather than account-wide: several exchanges require a
    symbol for fetchOpenOrders, and a silent failure here would report a clean
    account while orders sat on the book.
    """
    found = []
    problems = []
    for symbol in symbols:
        try:
            orders = client.fetch_open_orders(symbol) or []
        except Exception as exc:
            problems.append(f"{symbol}: {type(exc).__name__}: {exc}")
            continue
        for order in orders:
            found.append(dict(order))
    return found, problems


def free_balances(client):
    """{currency: free_amount} from the venue, non-zero only."""
    payload = client.fetch_balance() or {}
    free = payload.get("free") or {}
    balances = {}
    for currency, amount in free.items():
        value = D(amount)
        if value > ZERO:
            balances[str(currency)] = value
    return balances


def clock_skew_ms(client, clock=time.time):
    """Local clock minus exchange clock, in ms, or None if unavailable.

    Signed requests carry a timestamp and exchanges reject ones that drift too
    far. On Windows this is a routine failure after a laptop sleeps, and the
    error it produces ("Timestamp for this request is outside of the recvWindow")
    looks like an API-key problem, which sends people down the wrong path.
    """
    fetch = getattr(client, "fetch_time", None)
    if not callable(fetch):
        return None
    try:
        remote = fetch()
    except Exception:
        return None
    if remote is None:
        return None
    return int(clock() * 1000 - float(remote))


def balance_drift(expected, actual, prices=None,
                  tolerance_pct=DEFAULT_BALANCE_TOLERANCE_PCT,
                  dust_usdt=DEFAULT_DUST_USDT, quote="USDT"):
    """Currencies whose real balance disagrees with the bot's expectation.

    Differences worth less than `dust_usdt` are ignored: an 0.4 USDT gap is a
    fee rounding, and reporting it every restart trains operators to skip the
    report entirely.
    """
    priced = {str(k): D(v) for k, v in (prices or {}).items()}
    drifts = []
    for currency in sorted(set(expected) | set(actual)):
        want = D(expected.get(currency))
        have = D(actual.get(currency))
        gap = have - want
        if gap == ZERO:
            continue
        rate = D(1) if currency == quote else priced.get(currency)
        worth = abs(gap) * rate if rate and rate > ZERO else None
        if worth is not None and worth < D(dust_usdt):
            continue
        relative = abs(pct_change(want, have)) if want > ZERO else None
        if relative is not None and relative <= D(tolerance_pct):
            continue
        drifts.append({
            "currency": currency,
            "expected": want,
            "actual": have,
            "gap": gap,
            "gap_pct": relative,
            "gap_value_quote": worth,
        })
    return drifts


def _describe_order(order):
    filled = D(order.get("filled"))
    amount = D(order.get("amount"))
    return (f"{order.get('side', '?')} {order.get('amount', '?')} "
            f"{order.get('symbol', '?')} at {order.get('price', 'market')} "
            f"({float(filled)}/{float(amount)} filled, status "
            f"{order.get('status', 'unknown')}, id {order.get('id', '?')})")


def startup_check(clients, symbols, expected_balances=None, prices=None,
                  pending_records=None, max_clock_skew_ms=2000,
                  clock=time.time, quote="USDT"):
    """Ask every venue what is outstanding before the loop is allowed to start.

    `clients` is {exchange: ccxt_client}. `expected_balances` is
    {exchange: {currency: amount}} from the last persisted snapshot, or None to
    skip the balance comparison on a first run. `pending_records` are rows the
    database still lists as unresolved — the bot's own memory of a trade that
    did not finish.
    """
    discrepancies = []
    errors = []

    for record in (pending_records or []):
        discrepancies.append(Discrepancy(
            kind="unresolved_record",
            exchange=str(record.get("exchange") or record.get("buy_exchange") or ""),
            detail=(f"the database still lists this as unresolved: "
                    f"{record.get('reason') or record.get('detail') or record}"),
            data={str(k): str(v) for k, v in dict(record).items()}))

    for exchange, client in (clients or {}).items():
        try:
            orders, problems = open_orders_for(client, symbols)
        except Exception as exc:
            errors.append(f"{exchange}: could not list open orders "
                          f"({type(exc).__name__}: {exc})")
            continue
        for problem in problems:
            errors.append(f"{exchange}: {problem}")
        for order in orders:
            discrepancies.append(Discrepancy(
                kind="open_order", exchange=exchange,
                detail=f"resting order on {exchange}: {_describe_order(order)}",
                data={"id": str(order.get("id") or ""),
                      "symbol": str(order.get("symbol") or ""),
                      "side": str(order.get("side") or ""),
                      "amount": float(D(order.get("amount"))),
                      "filled": float(D(order.get("filled")))}))

        skew = clock_skew_ms(client, clock=clock)
        if skew is not None and abs(skew) > int(max_clock_skew_ms):
            discrepancies.append(Discrepancy(
                kind="clock_skew", exchange=exchange,
                detail=(f"local clock is {skew}ms from {exchange}'s, past the "
                        f"{max_clock_skew_ms}ms limit - signed requests will be "
                        f"rejected until the system clock is resynced"),
                data={"skew_ms": skew}))

        if expected_balances is None:
            continue
        try:
            actual = free_balances(client)
        except Exception as exc:
            errors.append(f"{exchange}: could not read balances "
                          f"({type(exc).__name__}: {exc})")
            continue
        for drift in balance_drift(expected_balances.get(exchange, {}), actual,
                                   prices=prices, quote=quote):
            discrepancies.append(Discrepancy(
                kind="balance_drift", exchange=exchange,
                detail=(f"{exchange} {drift['currency']}: expected "
                        f"{float(drift['expected'])}, found {float(drift['actual'])} "
                        f"({float(drift['gap']):+})"),
                blocking=False,
                data={k: (float(v) if isinstance(v, Decimal) else v)
                      for k, v in drift.items()}))

    return RecoveryReport(checked_at=clock(),
                          exchanges=tuple(sorted(clients or {})),
                          discrepancies=tuple(discrepancies),
                          errors=tuple(errors))


def cancel_open_orders(clients, symbols, confirm=False):
    """Cancel everything resting. Requires `confirm=True` to do anything.

    The flag is not ceremony. Cancelling is destructive in a way that is easy
    to under-estimate: a partially filled order that gets cancelled leaves the
    filled part as a real position, so "clean up the orders" can hand you an
    unhedged holding. The report tells you what exists; you decide.
    """
    if not confirm:
        return {"cancelled": [], "failed": [],
                "note": "no action taken: call with confirm=True to cancel"}
    cancelled = []
    failed = []
    for exchange, client in (clients or {}).items():
        orders, _problems = open_orders_for(client, symbols)
        for order in orders:
            identifier = order.get("id")
            symbol = order.get("symbol")
            try:
                client.cancel_order(identifier, symbol)
                cancelled.append({"exchange": exchange, "id": str(identifier),
                                  "symbol": str(symbol),
                                  "filled": float(D(order.get("filled")))})
            except Exception as exc:
                failed.append({"exchange": exchange, "id": str(identifier),
                               "symbol": str(symbol),
                               "error": f"{type(exc).__name__}: {exc}"})
    return {"cancelled": cancelled, "failed": failed,
            "note": ("check for partial fills: any cancelled order with "
                     "filled > 0 left a real position behind")}


def plan_unwind(exchange, currency, quantity, quote="USDT", prices=None):
    """Describe the order that would flatten a stranded holding, without sending it.

    Returned as a plan rather than executed so the decision to sell at market —
    which realizes the loss immediately — stays with a human. Feed the result to
    `orders.submit_market_order` if that is what you want.
    """
    amount = D(quantity)
    symbol = f"{currency}/{quote}"
    price = D((prices or {}).get(currency), default=None)
    estimate = amount * price if price and price > ZERO else None
    return {
        "exchange": exchange,
        "symbol": symbol,
        "side": "sell",
        "quantity": float(amount),
        "estimated_proceeds": float(estimate) if estimate is not None else None,
        "note": (f"market sell {float(amount)} {currency} on {exchange} to return "
                 f"to {quote}. This realizes the loss now; holding instead is a "
                 f"directional bet the strategy never intended to take."),
    }
