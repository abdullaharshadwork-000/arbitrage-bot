"""Pure, conservative exposure checks for authenticated spot-account snapshots.

The caller must collect every account asset and pass the expected venues. This
module only evaluates observations; it never submits orders or sells holdings.
"""

from collections.abc import Mapping
from decimal import Decimal, DecimalException, localcontext
import math


ZERO = Decimal("0")
HUNDRED = Decimal("100")
ROUNDING_TOLERANCE = Decimal("1e-12")


def _number(value, label):
    if value is None or isinstance(value, bool):
        raise ValueError(f"Missing or invalid {label}.")
    try:
        result = Decimal(str(value))
    except (DecimalException, TypeError, ValueError):
        raise ValueError(f"Missing or invalid {label}.") from None
    if not result.is_finite() or result < ZERO:
        raise ValueError(f"Non-finite or negative {label}.")
    return result


def _consistent(actual, expected, label):
    # CCXT and the existing account summary cross a float boundary. Permit
    # relative representation noise, never use it to relax the exposure cap.
    if abs(actual - expected) > max(abs(actual), abs(expected)) * ROUNDING_TOLERANCE:
        raise ValueError(f"Inconsistent {label}.")


def snapshot(balances, valuation, max_exposure_pct, pending_notional=0, *,
             expected_exchanges=None):
    """Return an allow/refuse decision and JSON-safe exposure measurements.

    ``balances`` is ``{venue: {asset: {free, used, total,
    value_free_usdt, value_used_usdt}}}``; ``valuation`` contains aggregate
    ``free_usdt``, ``used_usdt`` and ``total_usdt``. Non-USDT assets need a
    positive valuation for every positive free/used holding, including locked
    inventory. Empty, failed, incomplete and unvalued observations fail closed.

    ``pending_notional`` is the extra USDT value that a first buy leg could
    leave invested. It is added to current inventory without crediting a future
    sell leg or increasing equity. The cap permits equality. Numeric decisions
    use Decimal; floats are only produced for the returned dashboard report.
    """
    result = {
        "allowed": False,
        "reason": "",
        "total_inventory_usdt": None,
        "total_equity_usdt": None,
        "exposure_pct": None,
        "projected_exposure_pct": None,
        "max_exposure_pct": None,
        "headroom_usdt": 0.0,
    }
    try:
        with localcontext() as context:
            context.prec = 50
            cap = _number(max_exposure_pct, "maximum exposure percentage")
            pending = _number(pending_notional, "pending notional")
            if cap > HUNDRED:
                raise ValueError("Maximum exposure percentage must be between 0 and 100.")
            result["max_exposure_pct"] = float(cap)
            if not isinstance(balances, Mapping) or not balances:
                raise ValueError("Live balances are unavailable.")
            if expected_exchanges is not None:
                expected = ([expected_exchanges] if isinstance(expected_exchanges, str)
                            else list(expected_exchanges))
                if not expected or any(exchange not in balances for exchange in expected):
                    raise ValueError("Live balances are missing an expected venue.")
            if not isinstance(valuation, Mapping) or "error" in valuation:
                raise ValueError("Live account valuation is unavailable.")

            inventory = total_free = total_used = ZERO
            for exchange, accounts in balances.items():
                if not isinstance(accounts, Mapping) or not accounts or "error" in accounts:
                    raise ValueError(f"Live balances are unavailable for {exchange}.")
                for asset, account in accounts.items():
                    label = f"{exchange} {asset}"
                    if not isinstance(asset, str) or not asset or not isinstance(account, Mapping):
                        raise ValueError(f"Invalid balance for {label}.")
                    free = _number(account.get("free"), f"{label} free balance")
                    used = _number(account.get("used"), f"{label} used balance")
                    total = _number(account.get("total"), f"{label} total balance")
                    units = free + used
                    _consistent(total, units, f"{label} balance totals")
                    values = []
                    for kind, quantity in (("free", free), ("used", used)):
                        key = f"value_{kind}_usdt"
                        if asset == "USDT":
                            value = quantity
                            if key in account:
                                _consistent(_number(account[key], f"{label} {key}"),
                                            quantity, f"{label} {key}")
                        elif key not in account and quantity == ZERO:
                            value = ZERO
                        else:
                            value = _number(account.get(key), f"{label} {key}")
                            if quantity > ZERO and value <= ZERO:
                                raise ValueError(f"Unvalued live inventory for {label}.")
                            if quantity == ZERO and value != ZERO:
                                raise ValueError(f"Inconsistent valuation for {label}.")
                        values.append(value)
                    free_value, used_value = values
                    total_free += free_value
                    total_used += used_value
                    if asset != "USDT":
                        # Respect a slightly larger reported total instead of
                        # dropping even a rounding-sized amount of inventory.
                        asset_value = free_value + used_value
                        if total > units:
                            asset_value *= total / units
                        inventory += asset_value

            equity = total_free + total_used
            for key, expected_value in (("free_usdt", total_free),
                                        ("used_usdt", total_used),
                                        ("total_usdt", equity)):
                _consistent(_number(valuation.get(key), f"account {key}"),
                            expected_value, f"account {key}")
            if equity <= ZERO:
                # A complete, authenticated empty account is not a failed
                # request. Publish its zero equity so drawdown can latch.
                result.update(total_equity_usdt=0.0, total_inventory_usdt=0.0)
                raise ValueError("Live account equity must be positive.")

            current_pct = inventory / equity * HUNDRED
            projected_pct = (inventory + pending) / equity * HUNDRED
            measurements = {
                "total_inventory_usdt": inventory,
                "total_equity_usdt": equity,
                "exposure_pct": current_pct,
                "projected_exposure_pct": projected_pct,
                "headroom_usdt": max(ZERO, equity * cap / HUNDRED - inventory),
            }
            floats = {key: float(value) for key, value in measurements.items()}
            if not all(math.isfinite(value) for value in floats.values()):
                raise ValueError("Live account measurements exceed supported numeric bounds.")
            result.update(floats)
            # Compare notionals directly so a rounded percentage never permits
            # an order infinitesimally above the configured boundary.
            result["allowed"] = (inventory + pending) * HUNDRED <= equity * cap
            if not result["allowed"]:
                result["reason"] = (
                    f"Projected live inventory exposure {projected_pct:.4f}% "
                    f"exceeds the {cap}% maximum.")
    except ValueError as exc:
        result["reason"] = str(exc)
    except (TypeError, DecimalException, OverflowError):
        result["reason"] = "Invalid live account exposure measurements."
    return result
