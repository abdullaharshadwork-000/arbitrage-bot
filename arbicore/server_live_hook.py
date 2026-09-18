"""Hooks for server.py to auto-wire agent live execution.

Safe defaults:
* No-op unless ARBICORE_AGENT_LIVE_EXEC=1
* Requires a real execution engine with live clients
* Canary fraction from ARBICORE_AGENT_CANARY (default 0.05)
* Never raises into the scan loop
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from .agent_live_wire import (
    get_live_executor,
    live_wire_snapshot,
    process_handoff_queue,
    register_live_wire,
)
from .live_exec import live_exec_enabled
from .live_place import make_place_fn

log = logging.getLogger("arbicore.live_hook")


def _canary_fraction() -> float:
    raw = os.environ.get("ARBICORE_AGENT_CANARY", "0.05").strip()
    try:
        value = float(raw)
    except ValueError:
        value = 0.05
    return max(0.0, min(1.0, value))


def _pick_client(engine: Any) -> tuple[Optional[Any], str]:
    clients = getattr(engine, "clients", None) or {}
    if not isinstance(clients, dict) or not clients:
        return None, ""
    # Prefer binance when present; otherwise first client
    if "binance" in clients:
        return clients["binance"], "binance"
    name = next(iter(clients))
    return clients[name], str(name)


def maybe_register_from_engine(
    engine: Any,
    *,
    live_guard: Any = None,
    force: bool = False,
) -> bool:
    """Register live wire from RealExecutionEngine if live exec is enabled.

    Returns True when a wire is (already) registered.
    """
    try:
        if not live_exec_enabled() and not force:
            return get_live_executor() is not None
        if get_live_executor() is not None and not force:
            return True
        client, exchange = _pick_client(engine)
        if client is None:
            log.warning("agent live wire: engine has no clients")
            return False
        place_fn = make_place_fn(client, exchange)

        # Prefer engine methods when available (normalization + REAL_TRADING_ENABLED)
        place_buy = getattr(engine, "place_market_buy_quantity", None)
        place_sell = getattr(engine, "place_market_sell", None)
        if callable(place_buy) and callable(place_sell):
            def place_fn(symbol: str, side: str, quantity: float):
                side = (side or "").lower()
                if side == "buy":
                    raw = place_buy(exchange, symbol, quantity)
                elif side == "sell":
                    raw = place_sell(exchange, symbol, quantity)
                else:
                    raise ValueError(f"invalid side {side!r}")
                if isinstance(raw, dict):
                    return {
                        "order_id": str(raw.get("id") or raw.get("order_id") or ""),
                        "filled_quantity": float(
                            raw.get("filled") or raw.get("amount") or quantity
                        ),
                        "average_price": float(
                            raw.get("average") or raw.get("price") or 0
                        ),
                        "status": str(raw.get("status") or ""),
                        "exchange": exchange,
                    }
                return {"filled_quantity": float(quantity), "exchange": exchange}

        register_live_wire(
            place_fn,
            live_guard=live_guard,
            canary_fraction=_canary_fraction(),
        )
        log.info(
            "agent live wire registered exchange=%s canary=%.4f",
            exchange,
            _canary_fraction(),
        )
        return True
    except Exception as exc:
        log.warning("agent live wire registration failed: %s", exc)
        return False


def maybe_process_handoff(*, max_items: int = 1) -> list:
    """Drain handoff queue when live exec is on. Never raises."""
    try:
        if not live_exec_enabled():
            return []
        if get_live_executor() is None:
            return []
        return process_handoff_queue(max_items=max_items)
    except Exception as exc:
        log.warning("process_handoff failed: %s", exc)
        return []


def snapshot() -> dict:
    try:
        return live_wire_snapshot()
    except Exception as exc:
        return {"error": str(exc)}


__all__ = [
    "maybe_register_from_engine",
    "maybe_process_handoff",
    "snapshot",
]
