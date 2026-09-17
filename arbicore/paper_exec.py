"""Paper execution adapter.

Phase 24.

Accepts a TradeProposal + CriticDecision and, only when the critic approves,
simulates a fill against the last price. Never talks to a real exchange.
Never bypasses LiveModeGuard for real orders.

This is the safe place to practice the full decision → execution path before
any live capital is involved.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from .critic import CriticDecision
from .domain import OperatingMode, TradeProposal


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class PaperFill:
    id: str
    proposal_id: str
    symbol: str
    action: str
    quantity: float
    fill_price: float
    fee: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    status: str = "filled"
    mode: str = "paper"
    reason: str = ""
    created_at: datetime = field(default_factory=_utc_now)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        return data


@dataclass(frozen=True)
class PaperExecResult:
    accepted: bool
    fill: Optional[PaperFill] = None
    reject_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "fill": self.fill.as_dict() if self.fill else None,
            "reject_reason": self.reject_reason,
        }


class PaperExecutor:
    """Simulate fills for approved proposals only."""

    def __init__(
        self,
        *,
        fee_pct: float = 0.10,
        slippage_pct: float = 0.05,
        equity: float = 10_000.0,
        mode: OperatingMode = OperatingMode.PAPER,
    ):
        if mode is OperatingMode.LIVE:
            raise ValueError("PaperExecutor cannot run in LIVE mode")
        self.fee_pct = float(fee_pct)
        self.slippage_pct = float(slippage_pct)
        self.equity = float(equity)
        self.mode = mode
        self.fills: list[PaperFill] = []

    def execute(
        self,
        proposal: TradeProposal,
        critique: CriticDecision,
        *,
        last_price: Optional[float] = None,
    ) -> PaperExecResult:
        if critique.result == "REJECT":
            return PaperExecResult(False, reject_reason="critic REJECT")
        if critique.result == "WARN":
            return PaperExecResult(
                False, reject_reason="critic WARN – paper adapter requires APPROVE"
            )
        if proposal.action in ("NO_TRADE", "HOLD"):
            return PaperExecResult(False, reject_reason="no position change requested")
        if proposal.action not in ("BUY", "SELL"):
            return PaperExecResult(False, reject_reason=f"unsupported action {proposal.action}")

        price = float(
            last_price
            if last_price is not None
            else (float(proposal.entry_price) if proposal.entry_price is not None else 0.0)
        )
        if price <= 0:
            return PaperExecResult(False, reject_reason="invalid price")

        risk_frac = float(proposal.requested_risk_fraction or 0)
        if risk_frac <= 0:
            return PaperExecResult(False, reject_reason="zero risk fraction")

        fee = self.fee_pct / 100.0
        slip = self.slippage_pct / 100.0
        if proposal.action == "BUY":
            fill_price = price * (1 + fee + slip)
        else:
            fill_price = price * (1 - fee - slip)

        notional = self.equity * risk_frac
        quantity = notional / fill_price if fill_price > 0 else 0.0
        if quantity <= 0:
            return PaperExecResult(False, reject_reason="zero quantity")

        fee_paid = notional * fee
        fill = PaperFill(
            id=f"pf_{uuid4().hex[:12]}",
            proposal_id=proposal.id,
            symbol=proposal.symbol,
            action=proposal.action,
            quantity=round(quantity, 8),
            fill_price=round(fill_price, 8),
            fee=round(fee_paid, 6),
            stop_loss=float(proposal.stop_loss) if proposal.stop_loss is not None else None,
            take_profit=float(proposal.take_profit) if proposal.take_profit is not None else None,
            mode=self.mode.value,
            reason="paper fill after critic APPROVE",
        )
        self.fills.append(fill)
        return PaperExecResult(True, fill=fill)

    def snapshot(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "equity": self.equity,
            "fill_count": len(self.fills),
            "recent": [f.as_dict() for f in self.fills[-20:]],
        }


__all__ = ["PaperFill", "PaperExecResult", "PaperExecutor"]
