"""Track agent live fills and propose SL/TP exits.

Does not place orders itself. Exit intents go through the same
Risk Kernel + LiveExecutor path as entries.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from .live_exec import LiveExecResult
from .risk_adapter import ApprovedOrderRequest


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class AgentPosition:
    id: str
    symbol: str
    side: str  # long (bought) | short (sold)
    quantity: float
    entry_price: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    strategy_id: str = ""
    strategy_version: str = ""
    request_id: str = ""
    order_id: str = ""
    status: str = "open"  # open | closed | exit_pending
    opened_at: datetime = field(default_factory=_utc_now)
    closed_at: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["opened_at"] = self.opened_at.isoformat()
        data["closed_at"] = self.closed_at.isoformat() if self.closed_at else None
        return data


class AgentPositionBook:
    """In-memory book of agent-managed positions."""

    def __init__(self, max_closed: int = 200):
        self.open: dict[str, AgentPosition] = {}
        self.closed: list[AgentPosition] = []
        self.max_closed = max_closed

    def open_from_fill(
        self,
        result: LiveExecResult,
        *,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        strategy_id: str = "",
        strategy_version: str = "",
    ) -> Optional[AgentPosition]:
        if not result.executed or result.quantity <= 0:
            return None
        side = "long" if (result.side or "").lower() == "buy" else "short"
        pos = AgentPosition(
            id=f"apos_{uuid4().hex[:12]}",
            symbol=result.symbol,
            side=side,
            quantity=float(result.quantity),
            entry_price=float(result.average_price or 0),
            stop_loss=stop_loss,
            take_profit=take_profit,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            request_id=result.request_id,
            order_id=result.order_id,
        )
        self.open[pos.id] = pos
        return pos

    def mark_exit_pending(self, pos_id: str, reason: str) -> Optional[AgentPosition]:
        pos = self.open.get(pos_id)
        if not pos or pos.status != "open":
            return None
        pos.status = "exit_pending"
        pos.exit_reason = reason
        return pos

    def close(
        self,
        pos_id: str,
        *,
        exit_price: float,
        reason: str = "",
    ) -> Optional[AgentPosition]:
        pos = self.open.pop(pos_id, None)
        if pos is None:
            return None
        pos.status = "closed"
        pos.closed_at = _utc_now()
        pos.exit_price = float(exit_price)
        pos.exit_reason = reason or pos.exit_reason
        self.closed.append(pos)
        if len(self.closed) > self.max_closed:
            self.closed = self.closed[-self.max_closed :]
        return pos

    def check_exits(self, mid_prices: dict[str, float]) -> list[tuple[AgentPosition, str]]:
        """Return (position, reason) for open positions that hit SL or TP."""
        hits: list[tuple[AgentPosition, str]] = []
        for pos in list(self.open.values()):
            if pos.status != "open":
                continue
            mid = mid_prices.get(pos.symbol)
            if mid is None or mid <= 0:
                continue
            if pos.side == "long":
                if pos.stop_loss is not None and mid <= pos.stop_loss:
                    hits.append((pos, "stop_loss"))
                elif pos.take_profit is not None and mid >= pos.take_profit:
                    hits.append((pos, "take_profit"))
            else:
                if pos.stop_loss is not None and mid >= pos.stop_loss:
                    hits.append((pos, "stop_loss"))
                elif pos.take_profit is not None and mid <= pos.take_profit:
                    hits.append((pos, "take_profit"))
        return hits

    def exit_request(self, pos: AgentPosition) -> ApprovedOrderRequest:
        """Build a market exit request (opposite side)."""
        exit_side = "sell" if pos.side == "long" else "buy"
        notional = pos.quantity * (pos.entry_price or 0)
        return ApprovedOrderRequest(
            id=f"aor_exit_{uuid4().hex[:10]}",
            intent_id=f"exit_{pos.id}",
            symbol=pos.symbol,
            side=exit_side,
            order_type="market",
            notional_usdt=round(notional, 4),
            risk_fraction=0.0,
            entry_price=pos.entry_price,
            stop_loss=None,
            take_profit=None,
            strategy_id=pos.strategy_id,
            strategy_version=pos.strategy_version,
            mode="live",
            risk_reason=f"agent_exit:{pos.exit_reason or 'manual'}",
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "open_count": len(self.open),
            "closed_count": len(self.closed),
            "open": [p.as_dict() for p in self.open.values()],
            "closed_recent": [p.as_dict() for p in self.closed[-10:]],
        }


_book = AgentPositionBook()


def get_position_book() -> AgentPositionBook:
    return _book


__all__ = [
    "AgentPosition",
    "AgentPositionBook",
    "get_position_book",
]
