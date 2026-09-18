"""Self-improvement scorecard.

Tracks whether later decisions are healthier than earlier ones using
simple rolling metrics from experiences. Research/observability only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence


@dataclass(frozen=True)
class Scorecard:
    total_experiences: int
    early_win_rate: float
    late_win_rate: float
    early_avg_pnl: float
    late_avg_pnl: float
    win_rate_improvement: float
    pnl_improvement: float
    improving: bool
    reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _pnl(row: Any) -> Optional[float]:
    if isinstance(row, dict):
        v = row.get("realized_pnl")
    else:
        v = getattr(row, "realized_pnl", None)
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class ImprovementScorecard:
    """Compare first half vs second half of realized experiences."""

    def __init__(self, *,
                 min_samples: int = 20):
        self.min_samples = int(min_samples)

    def evaluate(self, experiences: Sequence[Any]) -> Scorecard:
        # chronological: memory returns DESC
        rows = list(reversed(list(experiences)))
        pnls = [(i, p) for i, r in enumerate(rows) if (p := _pnl(r)) is not None]
        n = len(pnls)
        if n < self.min_samples:
            return Scorecard(
                total_experiences=n,
                early_win_rate=0.0,
                late_win_rate=0.0,
                early_avg_pnl=0.0,
                late_avg_pnl=0.0,
                win_rate_improvement=0.0,
                pnl_improvement=0.0,
                improving=False,
                reason=f"insufficient samples ({n}/{self.min_samples})",
            )

        mid = n // 2
        early = [p for _, p in pnls[:mid]]
        late = [p for _, p in pnls[mid:]]

        def wr(vals: list[float]) -> float:
            return (sum(1 for v in vals if v > 0) / len(vals)) if vals else 0.0

        def avg(vals: list[float]) -> float:
            return (sum(vals) / len(vals)) if vals else 0.0

        e_wr, l_wr = wr(early), wr(late)
        e_pnl, l_pnl = avg(early), avg(late)
        wr_imp = l_wr - e_wr
        pnl_imp = l_pnl - e_pnl
        improving = wr_imp > 0 or pnl_imp > 0

        return Scorecard(
            total_experiences=n,
            early_win_rate=round(e_wr, 4),
            late_win_rate=round(l_wr, 4),
            early_avg_pnl=round(e_pnl, 6),
            late_avg_pnl=round(l_pnl, 6),
            win_rate_improvement=round(wr_imp, 4),
            pnl_improvement=round(pnl_imp, 6),
            improving=improving,
            reason=(
                "later window improved" if improving else "later window not improved"
            ),
            meta={"early_n": len(early), "late_n": len(late)},
        )


__all__ = ["Scorecard", "ImprovementScorecard"]
