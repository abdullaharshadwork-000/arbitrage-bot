"""Order submission and fill reconciliation.

The bug this module exists to kill: the original engine read `filled` straight
off the create-order response and fell back to `amount` (the size we *asked*
for) when it was absent. Many exchanges return a market order with
`filled: None` until you fetch it again, so the engine would either

  * treat the requested size as filled and then sell coin it never bought, or
  * see zero and raise a plain RuntimeError while a real position sat open,
    with no recovery record written because the error was not an
    UnhedgedPositionError.

Nothing here ever assumes a quantity. Either the exchange confirms the fill,
or we raise `OrderReconciliationError`, which the caller must treat as "a
position of unknown size may exist" — the only safe reading.
"""

import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from .money import D, ZERO

# ccxt statuses that mean "this order will not change again".
TERMINAL_STATUSES = {"closed", "canceled", "cancelled", "expired", "rejected"}

DEFAULT_POLL_TIMEOUT = 20.0   # seconds to keep asking before giving up
DEFAULT_POLL_INTERVAL = 0.35  # seconds between polls
DEFAULT_MAX_POLLS = 80        # hard call cap, so a stalled clock cannot
                              # turn one order into thousands of API calls
                              # and an IP ban


class OrderReconciliationError(RuntimeError):
    """The exchange accepted an order whose fill we could not confirm.

    Carries the identifiers needed to investigate by hand, because the caller
    cannot safely infer the position size.
    """

    def __init__(self, message, symbol=None, order_id=None,
                 client_order_id=None, exchange=None, raw=None):
        super().__init__(message)
        self.symbol = symbol
        self.order_id = order_id
        self.client_order_id = client_order_id
        self.exchange = exchange
        self.raw = raw or {}


@dataclass(frozen=True)
class Fill:
    """A confirmed execution. Every quantity here came from the exchange."""

    exchange: str
    symbol: str
    side: str
    order_id: str
    client_order_id: str
    status: str
    requested_quantity: Decimal
    filled_quantity: Decimal
    average_price: Decimal
    cost: Decimal
    fee_cost: Decimal = ZERO
    fee_currency: str = ""
    reconciled: bool = False
    polls: int = 0
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def unfilled_quantity(self):
        remainder = self.requested_quantity - self.filled_quantity
        return remainder if remainder > ZERO else ZERO

    @property
    def is_partial(self):
        return self.requested_quantity > ZERO and self.unfilled_quantity > ZERO

    def as_dict(self):
        return {
            "exchange": self.exchange,
            "symbol": self.symbol,
            "side": self.side,
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "status": self.status,
            "requested_quantity": float(self.requested_quantity),
            "filled_quantity": float(self.filled_quantity),
            "average_price": float(self.average_price),
            "cost": float(self.cost),
            "fee_cost": float(self.fee_cost),
            "fee_currency": self.fee_currency,
            "reconciled": self.reconciled,
            "polls": self.polls,
        }


def new_client_order_id(prefix="arbi"):
    """A unique id we control, so a timed-out submit can be looked up later.

    Without one, a network timeout on create_order is unresolvable: the order
    may or may not exist and there is no key to search for it by.
    """
    return f"{prefix}{uuid.uuid4().hex[:20]}"


def _extract_fee(payload):
    """Pull (cost, currency) out of either the `fee` dict or the `fees` list."""
    fee = payload.get("fee")
    if isinstance(fee, dict) and fee.get("cost") is not None:
        return D(fee.get("cost")), str(fee.get("currency") or "")
    fees = payload.get("fees")
    if isinstance(fees, list) and fees:
        total = ZERO
        currency = ""
        for entry in fees:
            if not isinstance(entry, dict):
                continue
            total += D(entry.get("cost"))
            currency = str(entry.get("currency") or currency)
        return total, currency
    return ZERO, ""


def _derive_quantities(payload):
    """Best-effort (filled, average, cost) from one order payload.

    Returns None for any value the payload does not actually establish. The
    caller keeps polling until all three are known — it never substitutes the
    requested size for the filled size.
    """
    filled = D(payload.get("filled"), default=None)
    cost = D(payload.get("cost"), default=None)
    average = D(payload.get("average"), default=None)
    if average is None or average <= ZERO:
        average = D(payload.get("price"), default=None)

    if filled is None and cost is not None and average and average > ZERO:
        filled = cost / average
    if cost is None and filled is not None and average and average > ZERO:
        cost = filled * average
    if (average is None or average <= ZERO) and filled and filled > ZERO and cost:
        average = cost / filled

    return filled, average, cost


def _supports(client, capability):
    """Whether ccxt advertises a capability, defaulting to attribute presence."""
    has = getattr(client, "has", None)
    if isinstance(has, dict) and capability in has:
        return bool(has[capability])
    method = {
        "fetchOrder": "fetch_order",
        "fetchMyTrades": "fetch_my_trades",
        "fetchOrderTrades": "fetch_order_trades",
        "fetchOpenOrders": "fetch_open_orders",
        "fetchClosedOrders": "fetch_closed_orders",
    }[capability]
    return callable(getattr(client, method, None))


def _aggregate_trades(client, symbol, order_id):
    """Rebuild a fill from the trade log when order lookup is unavailable.

    Some exchanges expire order records quickly but keep trades. Summing the
    trades for an order id gives an authoritative filled size and VWAP.
    """
    trades = []
    if _supports(client, "fetchOrderTrades"):
        try:
            trades = client.fetch_order_trades(order_id, symbol) or []
        except Exception:
            trades = []
    if not trades and _supports(client, "fetchMyTrades"):
        try:
            recent = client.fetch_my_trades(symbol) or []
            trades = [t for t in recent if str(t.get("order")) == str(order_id)]
        except Exception:
            trades = []
    if not trades:
        return None, None, None

    filled = ZERO
    cost = ZERO
    for trade in trades:
        filled += D(trade.get("amount"))
        trade_cost = D(trade.get("cost"), default=None)
        if trade_cost is None:
            trade_cost = D(trade.get("amount")) * D(trade.get("price"))
        cost += trade_cost
    if filled <= ZERO:
        return None, None, None
    return filled, cost / filled, cost


def reconcile_order(client, exchange, symbol, side, requested_quantity, created,
                    poll_timeout=DEFAULT_POLL_TIMEOUT,
                    poll_interval=DEFAULT_POLL_INTERVAL,
                    max_polls=DEFAULT_MAX_POLLS,
                    sleep=time.sleep, clock=time.monotonic):
    """Poll until the exchange states what actually filled.

    Returns a Fill whose numbers all originate from the exchange. Raises
    OrderReconciliationError if the fill is still unknown when the timeout
    expires — the caller must then treat the position as open and unknown.
    """
    payload = dict(created or {})
    order_id = payload.get("id")
    client_order_id = payload.get("clientOrderId") or ""
    requested = D(requested_quantity)
    deadline = clock() + poll_timeout
    polls = 0
    last_error = None

    while True:
        filled, average, cost = _derive_quantities(payload)
        status = str(payload.get("status") or "").lower()
        settled = status in TERMINAL_STATUSES

        if (not settled and requested > ZERO and filled is not None
                and filled >= requested):
            # An order cannot fill more than it asked for, so a report of the
            # full requested size is final even from an exchange that omits
            # `status`. Note the direction: this accepts the *exchange's*
            # number once it reaches the request, and never the reverse.
            settled = True

        if settled and filled is not None and filled <= ZERO:
            # A definite answer: the order closed without filling anything.
            return Fill(exchange, symbol, side, str(order_id or ""),
                        client_order_id, status or "closed", requested,
                        ZERO, ZERO, ZERO, *_extract_fee(payload),
                        reconciled=True, polls=polls, raw=payload)

        if (settled and filled is not None and filled > ZERO
                and average is not None and average > ZERO):
            fee_cost, fee_currency = _extract_fee(payload)
            return Fill(exchange, symbol, side, str(order_id or ""),
                        client_order_id, status or "closed", requested, filled,
                        average,
                        cost if cost is not None else filled * average,
                        fee_cost, fee_currency, reconciled=True, polls=polls,
                        raw=payload)

        if clock() >= deadline or not order_id or not _supports(client, "fetchOrder"):
            break
        if polls >= max_polls:
            break

        sleep(poll_interval)
        polls += 1
        try:
            payload = dict(client.fetch_order(order_id, symbol) or {})
        except Exception as exc:      # transient API error — keep trying
            last_error = exc

    # Order lookup exhausted. The trade log is the last authoritative source.
    if order_id:
        filled, average, cost = _aggregate_trades(client, symbol, order_id)
        if filled is not None:
            fee_cost, fee_currency = _extract_fee(payload)
            return Fill(exchange, symbol, side, str(order_id), client_order_id,
                        payload.get("status") or "closed", requested, filled,
                        average, cost, fee_cost, fee_currency,
                        reconciled=True, polls=polls, raw=payload)

    detail = f" Last lookup error: {last_error}." if last_error else ""
    raise OrderReconciliationError(
        f"Could not confirm the fill for {side} {symbol} on {exchange} after "
        f"{polls} lookups.{detail} Treat this position as OPEN and unknown; "
        f"check the exchange by order id {order_id or 'unknown'} / client id "
        f"{client_order_id or 'unknown'}.",
        symbol=symbol, order_id=order_id, client_order_id=client_order_id,
        exchange=exchange, raw=payload,
    )


def find_by_client_id(client, symbol, client_order_id):
    """Locate an order by the id we generated, for resolving a timed-out submit."""
    for capability, method in (("fetchOpenOrders", "fetch_open_orders"),
                               ("fetchClosedOrders", "fetch_closed_orders")):
        if not _supports(client, capability):
            continue
        try:
            orders = getattr(client, method)(symbol) or []
        except Exception:
            continue
        for order in orders:
            if str(order.get("clientOrderId") or "") == client_order_id:
                return dict(order)
    return None


def submit_market_order(client, exchange, symbol, side, quantity, params=None,
                        poll_timeout=DEFAULT_POLL_TIMEOUT,
                        poll_interval=DEFAULT_POLL_INTERVAL,
                        max_polls=DEFAULT_MAX_POLLS,
                        sleep=time.sleep, clock=time.monotonic):
    """Place a market order and return its confirmed Fill.

    A failure inside create_order is ambiguous — the request may have reached
    the exchange before the connection died. Before surfacing the error we
    search for our own clientOrderId; finding it turns an unknown into a
    normal reconciliation instead of an orphaned position.
    """
    client_order_id = new_client_order_id()
    request = dict(params or {})
    request.setdefault("clientOrderId", client_order_id)

    try:
        created = client.create_order(
            symbol, "market", side, float(D(quantity)), None, request)
    except Exception as exc:
        recovered = find_by_client_id(client, symbol, client_order_id)
        if recovered is None:
            raise OrderReconciliationError(
                f"Submitting {side} {symbol} on {exchange} failed ({exc}) and no "
                f"order with client id {client_order_id} was found. If the "
                f"exchange accepted it after the connection dropped, the "
                f"position is open and unrecorded - verify manually.",
                symbol=symbol, client_order_id=client_order_id,
                exchange=exchange,
            ) from exc
        created = recovered

    created.setdefault("clientOrderId", client_order_id)
    return reconcile_order(client, exchange, symbol, side, quantity, created,
                           poll_timeout=poll_timeout,
                           poll_interval=poll_interval,
                           max_polls=max_polls,
                           sleep=sleep, clock=clock)
