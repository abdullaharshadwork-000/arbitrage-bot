"""Trailing stop updates for agent positions.

Only tightens stops (never loosens). Exit still goes through LiveExecutor.
"""

from __future__ import annotations

from typing import Any, Optional

from .agent_positions import AgentPosition, AgentPositionBook, get_position_book


class TrailingStopManager:
    def __init__(self, trail_fraction: float = 0.015):
        self.trail_fraction = max(0.001, float(trail_fraction))

    def update(self, book: Optional[AgentPositionBook] = None,
               mid_prices: Optional[dict[str, float]] = None) -> list[dict[str, Any]]:
        b = book or get_position_book()
        mids = mid_prices or {}
        changes: list[dict[str, Any]] = []
        for pos in list(b.open.values()):
            if pos.status != "open":
                continue
            mid = mids.get(pos.symbol)
            if mid is None or mid <= 0:
                continue
            changed = self._trail_one(pos, float(mid))
            if changed:
                b._save(pos)  # noqa: SLF001 – persist tightened stop
                changes.append(changed)
        return changes

    def _trail_one(self, pos: AgentPosition, mid: float) -> Optional[dict[str, Any]]:
        frac = self.trail_fraction
        old_sl = pos.stop_loss
        if pos.side == "long":
            candidate = mid * (1.0 - frac)
            if pos.stop_loss is None or candidate > float(pos.stop_loss):
                # only if still below entry or already profitable trail
                if candidate < mid:
                    pos.stop_loss = round(candidate, 8)
                    return {
                        "position_id": pos.id,
                        "symbol": pos.symbol,
                        "side": pos.side,
                        "old_stop": old_sl,
                        "new_stop": pos.stop_loss,
                        "mid": mid,
                    }
        else:
            candidate = mid * (1.0 + frac)
            if pos.stop_loss is None or candidate < float(pos.stop_loss):
                if candidate > mid:
                    pos.stop_loss = round(candidate, 8)
                    return {
                        "position_id": pos.id,
                        "symbol": pos.symbol,
                        "side": pos.side,
                        "old_stop": old_sl,
                        "new_stop": pos.stop_loss,
                        "mid": mid,
                    }
        return None


__all__ = ["TrailingStopManager"]
