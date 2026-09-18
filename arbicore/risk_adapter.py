"""Risk Kernel adapter for agent OrderIntents.

Phase 22.

Takes an OrderIntent (from OrderIntentBridge after Critic APPROVE) and runs
it through the EXISTING RiskManager.check(). Does not call Binance.
Does not weaken limits. Does not auto-enable LIVE.

If risk allows, returns an ApprovedOrderRequest that *downstream* existing
execution code may use. This module never sends orders itself.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from .domain import OperatingMode
from .guards import LiveModeGuard
from .live_bridge import OrderIntent
from .risk import RiskDecision, RiskManager


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ApprovedOrderRequest:
    """Risk-cleared request – still not an exchange order."""

    id: str
    intent_id: str
    symbol: str
    side: str
    order_type: str
    notional_usdt: float
    risk_fraction: float
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    strategy_id: str = ""
    strategy_version: str = ""
    mode: str = "paper"
    risk_reason: str = "allowed"
    created_at: datetime = field(default_factory=_utc_now)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        return data


@dataclass(frozen=True)
class RiskAdapterResult:
    allowed: bool
    request: Optional[ApprovedOrderRequest] = None
    risk_decision: Optional[dict[str, Any]] = None
    reject_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "request": self.request.as_dict() if self.request else None,
            "risk_decision": self.risk_decision,
            "reject_reason": self.reject_reason,
        }


class RiskAdapter:
    """Evaluate OrderIntent against RiskManager + optional LiveModeGuard."""

    def __init__(
        self,
        risk_manager: RiskManager,
        *,
        live_guard: Optional[LiveModeGuard] = None,
        equity: float = 10_000.0,
        mode: OperatingMode = OperatingMode.PAPER,
    ):
        self.risk = risk_manager
        self.live_guard = live_guard
        self.equity = float(equity)
        self.mode = mode

    def evaluate(self, intent: OrderIntent) -> RiskAdapterResult:
        # LIVE intents require LiveModeGuard.can_place_real_orders()
        if self.mode is OperatingMode.LIVE or intent.mode == "live":
            if self.live_guard is None:
                return RiskAdapterResult(
                    False,
                    reject_reason="LIVE intent requires LiveModeGuard",
                )
            guard = self.live_guard.can_place_real_orders()
            if not guard.allowed:
                return RiskAdapterResult(
                    False,
                    reject_reason=f"LiveModeGuard: {guard.reason}",
                )

        risk_frac = float(intent.requested_risk_fraction or 0)
        if risk_frac <= 0:
            return RiskAdapterResult(False, reject_reason="zero risk fraction")

        notional = self.equity * risk_frac
        decision: RiskDecision = self.risk.check(notional, symbol=intent.symbol)
        risk_payload = {
            "allowed": bool(decision.allowed),
            "reason": decision.reason,
            "limit": decision.limit,
        }

        if not decision.allowed:
            return RiskAdapterResult(
                False,
                risk_decision=risk_payload,
                reject_reason=decision.reason or decision.limit or "risk rejected",
            )

        req = ApprovedOrderRequest(
            id=f"aor_{uuid4().hex[:12]}",
            intent_id=intent.id,
            symbol=intent.symbol,
            side=intent.side,
            order_type=intent.order_type,
            notional_usdt=round(notional, 4),
            risk_fraction=risk_frac,
            entry_price=intent.entry_price,
            stop_loss=intent.stop_loss,
            take_profit=intent.take_profit,
            strategy_id=intent.strategy_id,
            strategy_version=intent.strategy_version,
            mode=intent.mode,
            risk_reason=decision.reason or "allowed",
        )
        return RiskAdapterResult(True, request=req, risk_decision=risk_payload)


__all__ = ["ApprovedOrderRequest", "RiskAdapterResult", "RiskAdapter"]
