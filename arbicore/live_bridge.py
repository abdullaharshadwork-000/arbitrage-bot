"""Gated live order-intent bridge.

Phases 22–23 foundation.

Converts a Critic-APPROVED TradeProposal into a structured OrderIntent
that the EXISTING execution path may consider.

Hard rules:
* Never calls Binance / ccxt.
* Never bypasses Risk Kernel.
* Rejects unless Critic result is APPROVE.
* Rejects NO_TRADE / HOLD.
* LIVE still requires the operator's existing LiveModeGuard + real ack path.
* This module only builds an intent object for downstream existing code.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional
from uuid import uuid4

from .critic import CriticDecision
from .domain import OperatingMode, TradeProposal


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class OrderIntent:
    """Structured intent – not an exchange order."""

    id: str
    proposal_id: str
    symbol: str
    side: str  # buy | sell
    order_type: str  # market | limit
    requested_risk_fraction: float
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    strategy_id: str = ""
    strategy_version: str = ""
    mode: str = "paper"
    critic_result: str = ""
    created_at: datetime = field(default_factory=_utc_now)
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        return data


@dataclass(frozen=True)
class BridgeResult:
    accepted: bool
    intent: Optional[OrderIntent] = None
    reject_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "intent": self.intent.as_dict() if self.intent else None,
            "reject_reason": self.reject_reason,
        }


class OrderIntentBridge:
    """Build OrderIntent only when Critic APPROVES a directional proposal."""

    def __init__(self, *,
                 mode: OperatingMode = OperatingMode.PAPER):
        self.mode = mode

    def build(
        self,
        proposal: TradeProposal,
        critique: CriticDecision,
    ) -> BridgeResult:
        if critique.result != "APPROVE":
            return BridgeResult(
                False,
                reject_reason=f"critic={critique.result} (require APPROVE)",
            )
        if proposal.action in ("NO_TRADE", "HOLD"):
            return BridgeResult(False, reject_reason="no position change")
        if proposal.action not in ("BUY", "SELL"):
            return BridgeResult(False, reject_reason=f"unsupported action {proposal.action}")

        # LIVE intents are allowed to be *built* only if mode is LIVE,
        # but execution still depends on external LiveModeGuard + Risk Kernel.
        side = "buy" if proposal.action == "BUY" else "sell"
        risk = float(proposal.requested_risk_fraction or 0)
        if risk <= 0:
            return BridgeResult(False, reject_reason="zero risk fraction")

        intent = OrderIntent(
            id=f"oi_{uuid4().hex[:12]}",
            proposal_id=proposal.id,
            symbol=proposal.symbol,
            side=side,
            order_type=(proposal.entry_type or "MARKET").lower(),
            requested_risk_fraction=risk,
            entry_price=float(proposal.entry_price) if proposal.entry_price is not None else None,
            stop_loss=float(proposal.stop_loss) if proposal.stop_loss is not None else None,
            take_profit=float(proposal.take_profit) if proposal.take_profit is not None else None,
            strategy_id=proposal.strategy_id,
            strategy_version=proposal.strategy_version,
            mode=self.mode.value,
            critic_result=critique.result,
            notes=(
                "Intent only – must pass existing Risk Kernel and LiveModeGuard "
                "before any exchange order."
            ),
        )
        return BridgeResult(True, intent=intent)


__all__ = ["OrderIntent", "BridgeResult", "OrderIntentBridge"]
