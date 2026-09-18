"""Build a place_fn for LiveExecutor from an existing ccxt-like client.

Uses arbicore.orders.submit_market_order – the same reconciliation path as
the main bot. Does not create clients or hold secrets.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from .orders import submit_market_order


def make_place_fn(
    client: Any,
    exchange_name: str,
    *,
    params: Optional[dict] = None,
) -> Callable[[str, str, float], dict[str, Any]]:
    """Return place_fn(symbol, side, quantity) -> fill dict."""

    def place_fn(symbol: str, side: str, quantity: float) -> dict[str, Any]:
        fill = submit_market_order(
            client,
            exchange_name,
            symbol,
            side,
            quantity,
            params=dict(params or {}),
        )
        # Fill is a dataclass; normalize for LiveExecutor
        if hasattr(fill, "filled_quantity"):
            return {
                "order_id": getattr(fill, "order_id", ""),
                "filled_quantity": float(fill.filled_quantity),
                "average_price": float(getattr(fill, "average_price", 0) or 0),
                "status": getattr(fill, "status", ""),
                "exchange": exchange_name,
            }
        if isinstance(fill, dict):
            return fill
        return {"filled_quantity": float(quantity), "raw": str(fill)}

    return place_fn


__all__ = ["make_place_fn"]
