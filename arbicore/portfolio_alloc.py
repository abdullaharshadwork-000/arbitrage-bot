"""Allocate capital weights among APPROVED strategies.

May reduce exposure. Never exceeds Risk Kernel limits (caller enforces).
Cash / no-position is a valid allocation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass
class Allocation:
    strategy_id: str
    weight: float  # 0..1 of deployable capital
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AllocationPlan:
    allocations: list[Allocation] = field(default_factory=list)
    cash_weight: float = 1.0
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "allocations": [a.as_dict() for a in self.allocations],
            "cash_weight": self.cash_weight,
            "notes": self.notes,
        }


class PortfolioAllocator:
    """Simple score-based allocator.

    scores: strategy_id -> non-negative score (expectancy, regime fit, etc.)
    """

    def __init__(self, max_strategies: int = 5, min_cash: float = 0.1):
        self.max_strategies = max(1, int(max_strategies))
        self.min_cash = min(0.9, max(0.0, float(min_cash)))

    def allocate(self, scores: dict[str, float],
                 *,
                 drifted: Optional[set[str]] = None,
                 paused: Optional[set[str]] = None) -> AllocationPlan:
        drifted = drifted or set()
        paused = paused or set()
        usable = {
            k: max(0.0, float(v))
            for k, v in (scores or {}).items()
            if k not in paused and k not in drifted and float(v) > 0
        }
        if not usable:
            return AllocationPlan(cash_weight=1.0, notes="no eligible strategies – full cash")

        ranked = sorted(usable.items(), key=lambda kv: kv[1], reverse=True)[
            : self.max_strategies
        ]
        total = sum(s for _, s in ranked) or 1.0
        deployable = 1.0 - self.min_cash
        allocs = [
            Allocation(
                strategy_id=sid,
                weight=round(deployable * (score / total), 4),
                reason=f"score={score:.4f}",
            )
            for sid, score in ranked
        ]
        used = sum(a.weight for a in allocs)
        cash = max(self.min_cash, round(1.0 - used, 4))
        return AllocationPlan(
            allocations=allocs,
            cash_weight=cash,
            notes=f"deployed={used:.2%} cash={cash:.2%}",
        )


__all__ = ["Allocation", "AllocationPlan", "PortfolioAllocator"]
