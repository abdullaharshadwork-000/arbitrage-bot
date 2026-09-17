"""Research pipeline – end-to-end evaluation of a candidate strategy.

Phase 18 foundation.

Runs:
  create/fork strategy → backtest → walk-forward → stress → summary

Never places live orders. Never changes Risk Kernel limits.
Promotion remains a separate, human-gated step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from .backtest import BacktestResult, Backtester
from .domain import StrategyStatus, StrategyVersion
from .research import Experiment, Hypothesis, ResearchLab
from .strategy_registry import StrategyRegistry
from .validation import StressReport, StressTester, WalkForwardReport, WalkForwardValidator


@dataclass(frozen=True)
class ResearchPipelineReport:
    hypothesis_id: Optional[str]
    experiment_id: Optional[str]
    strategy_id: str
    strategy_version: str
    backtest: Optional[BacktestResult] = None
    walk_forward: Optional[WalkForwardReport] = None
    fee_stress: Optional[StressReport] = None
    slip_stress: Optional[StressReport] = None
    overall_passed: bool = False
    summary: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "experiment_id": self.experiment_id,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "backtest": self.backtest.as_dict() if self.backtest else None,
            "walk_forward": self.walk_forward.as_dict() if self.walk_forward else None,
            "fee_stress": self.fee_stress.as_dict() if self.fee_stress else None,
            "slip_stress": self.slip_stress.as_dict() if self.slip_stress else None,
            "overall_passed": self.overall_passed,
            "summary": self.summary,
            "meta": dict(self.meta),
        }


class ResearchPipeline:
    """Orchestrate research evaluation for one candidate strategy."""

    def __init__(
        self,
        registry: StrategyRegistry,
        lab: Optional[ResearchLab] = None,
        *,
        backtester: Optional[Backtester] = None,
        walk_forward: Optional[WalkForwardValidator] = None,
        stress: Optional[StressTester] = None,
    ):
        self.registry = registry
        self.lab = lab or ResearchLab()
        self.backtester = backtester or Backtester()
        self.walk_forward = walk_forward or WalkForwardValidator()
        self.stress = stress or StressTester()

    def evaluate_candidate(
        self,
        strategy: StrategyVersion,
        symbol: str,
        prices: Sequence[float],
        *,
        hypothesis: Optional[Hypothesis] = None,
        experiment: Optional[Experiment] = None,
    ) -> ResearchPipelineReport:
        bt = self.backtester.run(strategy, symbol, prices)
        wf = self.walk_forward.run(strategy, symbol, prices)
        fee_stress = self.stress.run_fee_spike(strategy, symbol, prices)
        slip_stress = self.stress.run_slippage_spike(strategy, symbol, prices)

        checks = [
            bt.meta.get("status") != "insufficient_bars",
            wf.passed if wf.total_windows > 0 else False,
            fee_stress.passed,
            slip_stress.passed,
        ]
        overall = all(checks)

        parts = [
            f"backtest_pnl={bt.total_pnl:.2f}",
            f"wf_passed={wf.passed} ({wf.profitable_windows}/{wf.total_windows})",
            f"fee_stress_passed={fee_stress.passed}",
            f"slip_stress_passed={slip_stress.passed}",
        ]
        summary = (
            "PASS – candidate cleared research gates"
            if overall
            else "FAIL – " + "; ".join(parts)
        )
        if overall:
            summary += " | " + "; ".join(parts)

        if overall:
            self.registry.set_status(strategy.id, StrategyStatus.VALIDATING)
        else:
            self.registry.set_status(strategy.id, StrategyStatus.FAILED_BACKTEST)

        return ResearchPipelineReport(
            hypothesis_id=hypothesis.id if hypothesis else None,
            experiment_id=experiment.id if experiment else None,
            strategy_id=strategy.id,
            strategy_version=strategy.version,
            backtest=bt,
            walk_forward=wf,
            fee_stress=fee_stress,
            slip_stress=slip_stress,
            overall_passed=overall,
            summary=summary,
            meta={"symbol": symbol, "bars": len(prices)},
        )


__all__ = ["ResearchPipelineReport", "ResearchPipeline"]
