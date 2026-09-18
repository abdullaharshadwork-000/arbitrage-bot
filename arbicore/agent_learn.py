"""Write live agent outcomes into ExperienceMemory (learn loop).

Best-effort; never raises into the trading path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional
from uuid import uuid4

from .agent_positions import AgentPosition
from .domain import Experience, OperatingMode


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def record_closed_position(pos: AgentPosition) -> bool:
    """Persist one closed agent position as an Experience."""
    try:
        from .scan_hook import get_memory

        memory = get_memory()
    except Exception:
        return False
    if memory is None:
        return False

    entry = float(pos.entry_price or 0)
    exit_px = float(pos.exit_price or 0)
    qty = float(pos.quantity or 0)
    if pos.side == "long":
        pnl = (exit_px - entry) * qty
        final_action = "SELL"
        proposed = "BUY"
    else:
        pnl = (entry - exit_px) * qty
        final_action = "BUY"
        proposed = "SELL"

    holding = None
    if pos.opened_at and pos.closed_at:
        try:
            holding = (pos.closed_at - pos.opened_at).total_seconds()
        except Exception:
            holding = None

    try:
        exp = Experience(
            id=f"exp_live_{uuid4().hex[:12]}",
            timestamp=_utc_now(),
            symbol=pos.symbol,
            mode=OperatingMode.LIVE,
            strategy_id=pos.strategy_id or None,
            strategy_version=pos.strategy_version or None,
            proposed_action=proposed,
            final_action=final_action,
            entry_price=Decimal(str(entry)) if entry else None,
            actual_entry=Decimal(str(entry)) if entry else None,
            stop_loss=Decimal(str(pos.stop_loss)) if pos.stop_loss is not None else None,
            take_profit=Decimal(str(pos.take_profit)) if pos.take_profit is not None else None,
            position_size=Decimal(str(qty)) if qty else None,
            realized_pnl=Decimal(str(round(pnl, 8))),
            holding_duration_seconds=holding,
            exit_reason=pos.exit_reason or None,
            risk_result="live_agent",
            critic_result="APPROVE",
            feature_snapshot={
                "source": "agent_live",
                "order_id": pos.order_id,
                "request_id": pos.request_id,
            },
        )
        return bool(memory.record_experience(exp))
    except Exception:
        return False


__all__ = ["record_closed_position"]
