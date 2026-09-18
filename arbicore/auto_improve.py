"""Self-improvement medium loop (daily-scale).

Detect drift → reflect → propose hypothesis. Never deploys to LIVE.
Promotion still requires PromotionGate + human approval.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ImprovementCycle:
    id: str
    strategy_id: str
    drift_severity: str = ""
    reflection: str = ""
    hypothesis: str = ""
    experiment_hint: str = ""
    status: str = "proposed"  # proposed | rejected | queued_research
    created_at: datetime = field(default_factory=_utc_now)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        return d


class AutoImproveLoop:
    def __init__(self, max_cycles: int = 100):
        self.cycles: list[ImprovementCycle] = []
        self.max_cycles = max_cycles

    def run_from_drift(
        self,
        strategy_id: str,
        *,
        severity: str = "medium",
        evidence: str = "",
    ) -> ImprovementCycle:
        reflection = (
            f"Strategy {strategy_id} shows {severity} drift. "
            f"Evidence: {evidence or 'rolling metrics deteriorated'}. "
            "Do not increase risk; research filters first."
        )
        hypothesis = (
            f"Tightening entry filters for {strategy_id} during elevated "
            f"volatility may restore expectancy."
        )
        experiment = (
            "Backtest ATR + relative-volume thresholds on last 6 months; "
            "walk-forward; stress with +2x slippage; shadow before paper."
        )
        cycle = ImprovementCycle(
            id=f"imp_{uuid4().hex[:10]}",
            strategy_id=strategy_id,
            drift_severity=severity,
            reflection=reflection,
            hypothesis=hypothesis,
            experiment_hint=experiment,
            status="queued_research",
        )
        self.cycles.append(cycle)
        if len(self.cycles) > self.max_cycles:
            self.cycles = self.cycles[-self.max_cycles :]
        return cycle

    def snapshot(self) -> dict[str, Any]:
        return {
            "count": len(self.cycles),
            "recent": [c.as_dict() for c in self.cycles[-10:]],
        }


_loop: Optional[AutoImproveLoop] = None


def get_auto_improve() -> AutoImproveLoop:
    global _loop
    if _loop is None:
        _loop = AutoImproveLoop()
    return _loop


__all__ = ["ImprovementCycle", "AutoImproveLoop", "get_auto_improve"]
