"""Walk-forward validation and stress checks.

Phases 12–13 foundation.

Walk-forward:
  Split history into sequential train/test windows.
  Evaluate on each test window without peeking ahead.

Stress:
  Apply simple shocks (fee spike, slippage spike, delayed fill)
  and measure degradation.

Research only. Never places live orders.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence

from .backtest import BacktestConfig, BacktestResult, Backtester
from .domain import StrategyVersion


@dataclass(frozen=True)
class WalkForwardWindow:
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    result: Optional[BacktestResult] = None

    def as_dict(self) -> dict[str, Any]:
        data = {
            "train_start": self.train_start,
            "train_end": self.train_end,
            "test_start": self.test_start,
            "test_end": self.test_end,
        }
        if self.result:
            data["result"] = self.result.as_dict()
        return data


@dataclass(frozen=True)
class WalkForwardReport:
    strategy_id: str
    strategy_version: str
    symbol: str
    windows: tuple[WalkForwardWindow, ...] = ()
    avg_pnl: float = 0.0
    avg_win_rate: float = 0.0
    avg_max_drawdown: float = 0.0
    profitable_windows: int = 0
    total_windows: int = 0
    passed: bool = False
    reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["windows"] = [w.as_dict() for w in self.windows]
        return data


@dataclass(frozen=True)
class StressReport:
    strategy_id: str
    strategy_version: str
    symbol: str
    baseline_pnl: float
    stressed_pnl: float
    degradation_pct: float
    scenario: str
    passed: bool
    reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class WalkForwardValidator:
    """Sequential train/test evaluation over a price series."""

    def __init__(
        self,
        *,
        train_bars: int = 60,
        test_bars: int = 20,
        step_bars: int = 20,
        min_profitable_ratio: float = 0.5,
        max_avg_drawdown: float = 0.25,
        backtester: Optional[Backtester] = None,
    ):
        self.train_bars = int(train_bars)
        self.test_bars = int(test_bars)
        self.step_bars = int(step_bars)
        self.min_profitable_ratio = float(min_profitable_ratio)
        self.max_avg_drawdown = float(max_avg_drawdown)
        self.backtester = backtester or Backtester()

    def run(
        self,
        strategy: StrategyVersion,
        symbol: str,
        prices: Sequence[float],
    ) -> WalkForwardReport:
        prices = [float(p) for p in prices if float(p) > 0]
        n = len(prices)
        windows: list[WalkForwardWindow] = []

        i = 0
        while i + self.train_bars + self.test_bars <= n:
            train_start = i
            train_end = i + self.train_bars
            test_start = train_end
            test_end = test_start + self.test_bars

            test_slice = prices[:test_end]
            result = self.backtester.run(strategy, symbol, test_slice)

            windows.append(WalkForwardWindow(
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
                result=result,
            ))
            i += self.step_bars

        if not windows:
            return WalkForwardReport(
                strategy_id=strategy.id,
                strategy_version=strategy.version,
                symbol=symbol,
                reason="insufficient data for any walk-forward window",
                passed=False,
            )

        pnls = [w.result.total_pnl for w in windows if w.result]
        win_rates = [w.result.win_rate for w in windows if w.result]
        dds = [w.result.max_drawdown for w in windows if w.result]
        profitable = sum(1 for p in pnls if p > 0)

        avg_pnl = sum(pnls) / len(pnls) if pnls else 0.0
        avg_wr = sum(win_rates) / len(win_rates) if win_rates else 0.0
        avg_dd = sum(dds) / len(dds) if dds else 0.0
        ratio = profitable / len(windows)

        passed = ratio >= self.min_profitable_ratio and avg_dd <= self.max_avg_drawdown
        reason = (
            f"profitable windows {ratio:.0%} (min {self.min_profitable_ratio:.0%}), "
            f"avg drawdown {avg_dd:.1%} (max {self.max_avg_drawdown:.0%})"
        )

        return WalkForwardReport(
            strategy_id=strategy.id,
            strategy_version=strategy.version,
            symbol=symbol,
            windows=tuple(windows),
            avg_pnl=round(avg_pnl, 4),
            avg_win_rate=round(avg_wr, 4),
            avg_max_drawdown=round(avg_dd, 4),
            profitable_windows=profitable,
            total_windows=len(windows),
            passed=passed,
            reason=reason,
            meta={"train_bars": self.train_bars, "test_bars": self.test_bars},
        )


class StressTester:
    """Apply fee/slippage shocks and measure performance degradation."""

    def __init__(
        self,
        *,
        max_degradation_pct: float = 50.0,
        baseline_config: Optional[BacktestConfig] = None,
    ):
        self.max_degradation_pct = float(max_degradation_pct)
        self.baseline_config = baseline_config or BacktestConfig()

    def run_fee_spike(
        self,
        strategy: StrategyVersion,
        symbol: str,
        prices: Sequence[float],
        *,
        fee_multiplier: float = 2.0,
    ) -> StressReport:
        baseline_bt = Backtester(self.baseline_config)
        baseline = baseline_bt.run(strategy, symbol, prices)

        stressed_cfg = BacktestConfig(
            initial_capital=self.baseline_config.initial_capital,
            fee_pct=self.baseline_config.fee_pct * fee_multiplier,
            slippage_pct=self.baseline_config.slippage_pct,
            risk_fraction=self.baseline_config.risk_fraction,
            min_bars=self.baseline_config.min_bars,
        )
        stressed_bt = Backtester(stressed_cfg)
        stressed = stressed_bt.run(strategy, symbol, prices)

        base_pnl = baseline.total_pnl
        stress_pnl = stressed.total_pnl
        if base_pnl == 0:
            degradation = 0.0 if stress_pnl >= 0 else 100.0
        else:
            degradation = max(0.0, (base_pnl - stress_pnl) / abs(base_pnl) * 100.0)

        passed = degradation <= self.max_degradation_pct
        return StressReport(
            strategy_id=strategy.id,
            strategy_version=strategy.version,
            symbol=symbol,
            baseline_pnl=round(base_pnl, 4),
            stressed_pnl=round(stress_pnl, 4),
            degradation_pct=round(degradation, 2),
            scenario=f"fee_x{fee_multiplier}",
            passed=passed,
            reason=(
                f"degradation {degradation:.1f}% "
                f"({'within' if passed else 'exceeds'} limit {self.max_degradation_pct:.0f}%)"
            ),
            meta={"fee_multiplier": fee_multiplier},
        )

    def run_slippage_spike(
        self,
        strategy: StrategyVersion,
        symbol: str,
        prices: Sequence[float],
        *,
        slip_multiplier: float = 3.0,
    ) -> StressReport:
        baseline_bt = Backtester(self.baseline_config)
        baseline = baseline_bt.run(strategy, symbol, prices)

        stressed_cfg = BacktestConfig(
            initial_capital=self.baseline_config.initial_capital,
            fee_pct=self.baseline_config.fee_pct,
            slippage_pct=self.baseline_config.slippage_pct * slip_multiplier,
            risk_fraction=self.baseline_config.risk_fraction,
            min_bars=self.baseline_config.min_bars,
        )
        stressed_bt = Backtester(stressed_cfg)
        stressed = stressed_bt.run(strategy, symbol, prices)

        base_pnl = baseline.total_pnl
        stress_pnl = stressed.total_pnl
        if base_pnl == 0:
            degradation = 0.0 if stress_pnl >= 0 else 100.0
        else:
            degradation = max(0.0, (base_pnl - stress_pnl) / abs(base_pnl) * 100.0)

        passed = degradation <= self.max_degradation_pct
        return StressReport(
            strategy_id=strategy.id,
            strategy_version=strategy.version,
            symbol=symbol,
            baseline_pnl=round(base_pnl, 4),
            stressed_pnl=round(stress_pnl, 4),
            degradation_pct=round(degradation, 2),
            scenario=f"slippage_x{slip_multiplier}",
            passed=passed,
            reason=(
                f"degradation {degradation:.1f}% "
                f"({'within' if passed else 'exceeds'} limit {self.max_degradation_pct:.0f}%)"
            ),
            meta={"slip_multiplier": slip_multiplier},
        )


__all__ = [
    "WalkForwardWindow",
    "WalkForwardReport",
    "WalkForwardValidator",
    "StressReport",
    "StressTester",
]
