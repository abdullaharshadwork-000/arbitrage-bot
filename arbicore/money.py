"""Decimal money arithmetic.

Floats cannot represent 0.1 exactly, so a long sequence of fee multiplications
drifts, and comparing a computed order size against an exchange's step size
gives false rejections. Every value that becomes an order size, a fee, a
balance, or a realized P&L figure goes through this module.

Prices arriving from ccxt are floats. Convert them at the boundary with `D()`
(which routes through `str` to avoid inheriting binary artifacts) and convert
back with `to_float()` only when handing data to JSON or the console.
"""

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, getcontext

# 34 significant digits: enough for 8-decimal crypto amounts multiplied by
# 8-decimal prices without intermediate rounding.
getcontext().prec = 34

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")


def D(value, default=ZERO):
    """Convert anything ccxt hands us into a Decimal.

    Floats go through `str` so that D(0.1) is exactly Decimal("0.1") rather
    than the 0.1000000000000000055511151231257827 that float carries.
    None and unparseable values collapse to `default` — exchange payloads
    routinely omit fields, and a missing field must not raise mid-order.
    """
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        return default
    if isinstance(value, float):
        return Decimal(str(value))
    try:
        return Decimal(value)
    except (ArithmeticError, TypeError, ValueError):
        return default


def to_float(value):
    """Boundary conversion for JSON payloads and printf-style formatting."""
    return float(D(value))


def step_from_precision(precision):
    """Normalize ccxt's two precision conventions into a step size.

    ccxt reports `market["precision"]["amount"]` either as a count of decimal
    places (Binance-style: 8) or as the step itself (Bitfinex-style: 0.001).
    Integers below 20 are treated as decimal places; anything else is already
    a step. Returns None when the exchange did not report precision, meaning
    "do not quantize".
    """
    if precision is None:
        return None
    value = D(precision, default=None)
    if value is None:
        return None
    if value <= 0:
        return None
    if value == value.to_integral_value() and value < 20:
        return ONE.scaleb(-int(value))
    return value


def floor_to_step(value, step):
    """Round DOWN to a multiple of `step`.

    Always down: rounding an order size up can exceed the balance that was
    just checked, and the exchange rejects the order after we have already
    committed to the other leg of the trade.
    """
    amount = D(value)
    if step is None:
        return amount
    increment = D(step)
    if increment <= ZERO:
        return amount
    return (amount / increment).to_integral_value(rounding=ROUND_DOWN) * increment


def round_money(value, places=8):
    """Round to `places` decimals for storage and display (nearest, not down)."""
    return D(value).quantize(ONE.scaleb(-places), rounding=ROUND_HALF_UP)


def after_fee(amount, fee_rate):
    """Amount remaining after a proportional taker fee."""
    return D(amount) * (ONE - D(fee_rate))


def net_spread_pct(buy_price, sell_price, buy_fee, sell_fee):
    """Profit percentage of a buy/sell pair, fees compounded not summed.

    The additive shortcut (gross_pct - 2*fee*100) overstates the edge, because
    the sell-side fee applies to the grossed-up proceeds rather than to the
    original notional. On a 0.1%/0.1% pair the error is small but it is always
    in the optimistic direction, which is the direction that loses money.
    """
    buy = D(buy_price)
    if buy <= ZERO:
        return ZERO
    ratio = (D(sell_price) / buy) * (ONE - D(buy_fee)) * (ONE - D(sell_fee))
    return (ratio - ONE) * HUNDRED


def pct_change(start, end):
    """Percentage change from `start` to `end`; ZERO when start is zero."""
    base = D(start)
    if base == ZERO:
        return ZERO
    return (D(end) - base) / base * HUNDRED
