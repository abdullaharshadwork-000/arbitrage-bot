"""Compare champion vs challenger on risk-adjusted metrics.

Does not auto-promote. PromotionGate + human approval still required.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional


@dataclass
class StrategyMetrics:
    strategy_id: str
    version: str = ""
    sharpe: float = 0.0
    sortino: float = 0.0
    max_drawdown_pct: float = 0.0
    expectancy: float = 0.0
    profit_factor: float = 0.0
    trade_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ChampionComparison:
    champion: StrategyMetrics
    challenger: StrategyMetrics
    challenger_preferred: bool
    reasons: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "champion": self.champion.as_dict(),
            "challenger": self.challenger.as_dict(),
            "challenger_preferred": self.challenger_preferred,
            "reasons": list(self.reasons),
        }


def compare(
    champion: StrategyMetrics,
    challenger: StrategyMetrics,
    *,
    min_trades: int = 30,
    max_dd_advantage: float = 2.0,
) -> ChampionComparison:
    reasons: list[str] = []
    prefer = True

    if challenger.trade_count < min_trades:
        prefer = False
        reasons.append(
            f"challenger trades {challenger.trade_count} < min {min_trades}"
        )

    if challenger.expectancy <= 0:
        prefer = False
        reasons.append("challenger expectancy not positive")

    if challenger.sharpe < champion.sharpe:
        prefer = False
        reasons.append(
            f"challenger Sharpe {challenger.sharpe:.3f} < champion {champion.sharpe:.3f}"
        )
    else:
        reasons.append(
            f"Sharpe improved {champion.sharpe:.3f} → {challenger.sharpe:.3f}"
        )

    if challenger.max_drawdown_pct > champion.max_drawdown_pct + max_dd_advantage:
        prefer = False
        reasons.append(
            f"challenger drawdown worse {challenger.max_drawdown_pct:.1f}% vs "
            f"{champion.max_drawdown_pct:.1f}%"
        )

    if prefer and not reasons:
        reasons.append("challenger meets comparison thresholds")

    return ChampionComparison(
        champion=champion,
        challenger=challenger,
        challenger_preferred=prefer,
        reasons=reasons,
    )


__all__ = ["StrategyMetrics", "ChampionComparison", "compare"]
