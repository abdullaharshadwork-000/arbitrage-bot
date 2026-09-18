"""Agent-driven live execution (gated).

Allows the agentic layer to place REAL exchange orders only when:

1. ARBICORE_AGENT_LIVE_EXEC=1
2. ApprovedOrderRequest already passed RiskManager.check
3. LiveModeGuard.can_place_real_orders() is True (mode=live, execution=real,
   exact real_trading_ack, credentials)
4. Optional canary fraction scales notional down
5. A place_fn is provided that uses the EXISTING exchange client / submit_market_order

Agents never hold API keys. This module never constructs a ccxt client itself.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from uuid import uuid4

from .guards import LiveModeGuard
from .risk_adapter import ApprovedOrderRequest

# place_fn(symbol, side, quantity) -> dict with at least filled_quantity / average_price / order_id
PlaceFn = Callable[[str, str, float], dict[str, Any]]


def live_exec_enabled(environ: Optional[dict] = None) -> bool:
    env = environ if environ is not None else os.environ
    return str(env.get("ARBICORE_AGENT_LIVE_EXEC", "")).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class LiveExecResult:
    executed: bool
    request_id: str = ""
    order_id: str = ""
    symbol: str = ""
    side: str = ""
    quantity: float = 0.0
    notional_usdt: float = 0.0
    average_price: float = 0.0
    canary_fraction: float = 1.0
    reject_reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    at: datetime = field(default_factory=_utc_now)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["at"] = self.at.isoformat()
        return data


class LiveExecutor:
    """Place a risk-cleared ApprovedOrderRequest via injected place_fn."""

    def __init__(
        self,
        place_fn: PlaceFn,
        *,
        live_guard: Optional[LiveModeGuard] = None,
        canary_fraction: float = 1.0,
        min_notional: float = 5.0,
        require_flag: bool = True,
    ):
        if place_fn is None or not callable(place_fn):
            raise ValueError("place_fn is required")
        self.place_fn = place_fn
        self.live_guard = live_guard
        self.canary_fraction = max(0.0, min(1.0, float(canary_fraction)))
        self.min_notional = float(min_notional)
        self.require_flag = bool(require_flag)

    def execute(self, request: ApprovedOrderRequest) -> LiveExecResult:
        if self.require_flag and not live_exec_enabled():
            return LiveExecResult(
                False,
                request_id=request.id,
                reject_reason="ARBICORE_AGENT_LIVE_EXEC is not enabled",
            )

        if self.live_guard is not None:
            decision = self.live_guard.can_place_real_orders()
            if not decision.allowed:
                return LiveExecResult(
                    False,
                    request_id=request.id,
                    reject_reason=f"LiveModeGuard: {decision.reason}",
                )

        frac = self.canary_fraction if self.canary_fraction > 0 else 0.0
        if frac <= 0:
            return LiveExecResult(
                False,
                request_id=request.id,
                reject_reason="canary fraction is zero",
            )

        notional = float(request.notional_usdt) * frac
        if notional < self.min_notional:
            return LiveExecResult(
                False,
                request_id=request.id,
                notional_usdt=notional,
                canary_fraction=frac,
                reject_reason=f"notional {notional:.4f} below min {self.min_notional}",
            )

        price = float(request.entry_price or 0.0)
        if price <= 0:
            return LiveExecResult(
                False,
                request_id=request.id,
                reject_reason="entry_price required to size quantity",
            )

        quantity = notional / price
        side = (request.side or "").lower()
        if side not in ("buy", "sell"):
            return LiveExecResult(
                False,
                request_id=request.id,
                reject_reason=f"invalid side {request.side!r}",
            )

        try:
            raw = self.place_fn(request.symbol, side, float(quantity)) or {}
        except Exception as exc:
            return LiveExecResult(
                False,
                request_id=request.id,
                symbol=request.symbol,
                side=side,
                quantity=quantity,
                notional_usdt=notional,
                canary_fraction=frac,
                reject_reason=f"place_fn error: {exc}",
            )

        filled = float(raw.get("filled_quantity") or raw.get("filled") or quantity)
        avg = float(raw.get("average_price") or raw.get("average") or price)
        order_id = str(raw.get("order_id") or raw.get("id") or "")

        return LiveExecResult(
            True,
            request_id=request.id,
            order_id=order_id or f"live_{uuid4().hex[:10]}",
            symbol=request.symbol,
            side=side,
            quantity=filled,
            notional_usdt=notional,
            average_price=avg,
            canary_fraction=frac,
            raw=dict(raw) if isinstance(raw, dict) else {"value": raw},
        )


__all__ = ["live_exec_enabled", "LiveExecResult", "LiveExecutor", "PlaceFn"]
