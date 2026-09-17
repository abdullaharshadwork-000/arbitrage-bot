"""Drift monitor.

Phase 28 foundation.

Compares recent experience metrics against a baseline window.
Research / observation only – never places orders.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Sequence, Union

from .domain import Experience

Row = Union[Experience, dict[str, Any]]


@dataclass(frozen=True)
class DriftReport:
    strategy_id: str
    baseline_count: int
    recent_count: int
    baseline_win_rate: float
    recent_win_rate: float
    baseline_avg_pnl: float
    recent_avg_pnl: float
    win_rate_delta: float
    pnl_delta: float
    drifted: bool
    severity: str
    reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sid(row: Row) -> Optional[str]:
    if isinstance(row, Experience):
        return row.strategy_id
    return row.get("strategy_id")


def _pnl(row: Row) -> Optional[float]:
    if isinstance(row, Experience):
        if row.realized_pnl is None:
            return None
        return float(row.realized_pnl)
    val = row.get("realized_pnl")
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


# fix forward ref
from typing import Optional  # noqa: E402


class DriftMonitor:
    def __init__(
        self,
        *,
        baseline_size: int = 30,
        recent_size: int = 15,
        win_rate_drop: float = 0.15,
        pnl_drop: float = 0.0,
    ):
        self.baseline_size = int(baseline_size)
        self.recent_size = int(recent_size)
        self.win_rate_drop = float(win_rate_drop)
        self.pnl_drop = float(pnl_drop)

    def evaluate(self, strategy_id: str, experiences: Sequence[Row]) -> DriftReport:
        rows = [e for e in experiences if _sid(e) == strategy_id]
        # chronological: memory returns DESC; reverse for time order
        rows = list(reversed(rows))
        n = len(rows)
        need = self.baseline_size + self.recent_size
        if n < need:
            return DriftReport(
                strategy_id=strategy_id,
                baseline_count=n,
                recent_count=0,
                baseline_win_rate=0.0,
                recent_win_rate=0.0,
                baseline_avg_pnl=0.0,
                recent_avg_pnl=0.0,
                win_rate_delta=0.0,
                pnl_delta=0.0,
                drifted=False,
                severity="none",
                reason=f"insufficient experiences ({n}/{need})",
            )

        baseline = rows[-(need):-self.recent_size]
        recent = rows[-self.recent_size:]

        def _stats(chunk: Sequence[Row]) -> tuple[float, float]:
            pnls = [p for p in (_pnl(e) for e in chunk) if p is not None]
            if not pnls:
                return 0.0, 0.0
            wins = sum(1 for p in pnls if p > 0)
            return wins / len(pnls), sum(pnls) / len(pnls)

        b_wr, b_pnl = _stats(baseline)
        r_wr, r_pnl = _stats(recent)
        wr_delta = r_wr - b_wr
        pnl_delta = r_pnl - b_pnl

        drifted = wr_delta <= -self.win_rate_drop or pnl_delta < self.pnl_drop
        if not drifted:
            severity, reason = "none", "within tolerance"
        elif wr_delta <= -self.win_rate_drop * 2:
            severity, reason = "severe", f"win-rate drop {wr_delta:.1%}"
        else:
            severity = "mild"
            reason = f"win-rate delta {wr_delta:.1%}, pnl delta {pnl_delta:.4f}"

        return DriftReport(
            strategy_id=strategy_id,
            baseline_count=len(baseline),
            recent_count=len(recent),
            baseline_win_rate=round(b_wr, 4),
            recent_win_rate=round(r_wr, 4),
            baseline_avg_pnl=round(b_pnl, 6),
            recent_avg_pnl=round(r_pnl, 6),
            win_rate_delta=round(wr_delta, 4),
            pnl_delta=round(pnl_delta, 6),
            drifted=drifted,
            severity=severity,
            reason=reason,
            meta={"baseline_size": self.baseline_size, "recent_size": self.recent_size},
        )


__all__ = ["DriftReport", "DriftMonitor"]
