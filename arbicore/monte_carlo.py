"""Simple Monte Carlo on trade PnL series.

Used in validation – never changes live risk limits.
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass
from typing import Any, Sequence


@dataclass
class MonteCarloReport:
    paths: int
    median_final_equity: float
    p5_final_equity: float
    p95_final_equity: float
    median_max_drawdown_pct: float
    p95_max_drawdown_pct: float
    prob_ruin: float  # equity below ruin_level
    starting_equity: float
    ruin_level: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_monte_carlo(
    trade_pnls: Sequence[float],
    *,
    starting_equity: float = 10_000.0,
    paths: int = 500,
    ruin_level: float = 0.5,
    seed: int = 42,
) -> MonteCarloReport:
    pnls = [float(x) for x in trade_pnls]
    if not pnls:
        return MonteCarloReport(
            paths=0,
            median_final_equity=starting_equity,
            p5_final_equity=starting_equity,
            p95_final_equity=starting_equity,
            median_max_drawdown_pct=0.0,
            p95_max_drawdown_pct=0.0,
            prob_ruin=0.0,
            starting_equity=starting_equity,
            ruin_level=ruin_level,
        )

    rng = random.Random(seed)
    finals: list[float] = []
    max_dds: list[float] = []
    ruins = 0
    floor = starting_equity * float(ruin_level)

    for _ in range(max(1, int(paths))):
        eq = float(starting_equity)
        peak = eq
        max_dd = 0.0
        order = pnls[:]
        rng.shuffle(order)
        for p in order:
            eq += p
            if eq > peak:
                peak = eq
            if peak > 0:
                dd = (peak - eq) / peak
                if dd > max_dd:
                    max_dd = dd
            if eq <= floor:
                ruins += 1
                break
        finals.append(eq)
        max_dds.append(max_dd * 100.0)

    finals.sort()
    max_dds.sort()
    n = len(finals)

    def pct(arr: list[float], p: float) -> float:
        if not arr:
            return 0.0
        i = min(n - 1, max(0, int(p * (n - 1))))
        return round(arr[i], 4)

    return MonteCarloReport(
        paths=n,
        median_final_equity=pct(finals, 0.5),
        p5_final_equity=pct(finals, 0.05),
        p95_final_equity=pct(finals, 0.95),
        median_max_drawdown_pct=pct(max_dds, 0.5),
        p95_max_drawdown_pct=pct(max_dds, 0.95),
        prob_ruin=round(ruins / n, 4) if n else 0.0,
        starting_equity=starting_equity,
        ruin_level=ruin_level,
    )


__all__ = ["MonteCarloReport", "run_monte_carlo"]
