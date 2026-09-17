"""Backtest & evaluation harness.

Phase 11 foundation.

Runs a strategy version over historical prices using the same FeatureEngine
and RegimeDetector as live. Produces transparent performance metrics.

Rules:
* Research only – never places live orders.
* Fees and slippage are explicit assumptions.
* Metrics are simple and auditable (no hidden magic).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Optional, Sequence

from .domain import StrategyVersion
from .features import FeatureEngine
from .regime import RegimeDetector
from .selection import StrategySelector
from .strategy_registry import StrategyRegistry


@dataclass(frozen=True)
class BacktestConfig:
    initial_capital: float = 10_000.0
    fee_pct: float = 0.10          # per side, percent
    slippage_pct: float = 0.05
    risk_fraction: float = 0.01    # fraction of equity risked per trade
    min_bars: int = 30


@dataclass(frozen=True)
class BacktestTrade:
    bar_index: int
    symbol: str
    action: str
    entry_price: float
    exit_price: float
    pnl: float
    regime: str
    reason: str = ""


@dataclass(frozen=True)
class BacktestResult:
    strategy_id: str
    strategy_version: str
    symbol: str
    bars: int
    trades: tuple[BacktestTrade, ...] = ()
    total_pnl: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    sharpe_proxy: float = 0.0
    final_equity: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["trades"] = [asdict(t) for t in self.trades]
        return data


class Backtester:
    """Minimal bar-based evaluator for research.

    This is intentionally simple. It does not claim production-grade
    realism; it exists so experiments and promotion gates have a common
    evaluation path that uses the same features/regime code as live.
    """

    def __init(
        self,
        config: Optional[BacktestConfig] = None,
        feature_engine: Optional[FeatureEngine] = None,
        regime_detector: Optional[RegimeDetector] = None,
    ):
        self.config = config or BacktestConfig()
        self.features = feature_engine or FeatureEngine()
        self.regime = regime_detector or RegimeDetector()

    def run(
        self,
        strategy: StrategyVersion,
        symbol: str,
        prices: Sequence[float],
        *,
        volumes: Optional[Sequence[float]] = None,
    ) -> BacktestResult:
        prices = [float(p) for p in prices if float(p) > 0]
        n = len(prices)
        if n < self.config.min_bars:
            return BacktestResult(
                strategy_id=strategy.id,
                strategy_version=strategy.version,
                symbol=symbol,
                bars=n,
                meta={"status": "insufficient_bars"},
            )

        equity = self.config.initial_capital
        peak = equity
        max_dd = 0.0
        trades: list[BacktestTrade] = []
        returns: list[float] = []
        position = 0.0
        entry_price = 0.0
        entry_bar = 0
        entry_regime = ""

        fee = self.config.fee_pct / 100.0
        slip = self.config.slippage_pct / 100.0

        for i in range(self.config.min_bars, n):
            window = prices[: i + 1]
            vol_window = volumes[: i + 1] if volumes is not None else None
            snap = self.features.compute(symbol, window, volumes=vol_window)
            regime = self.regime.classify(snap)
            price = prices[i]

            # Exit logic: simple mean-reversion / momentum placeholder
            # A real strategy would supply its own signal function.
            # Here we use a transparent momentum rule for demonstration.
            mom = snap.get("momentum_10", 0.0)

            if position == 0:
                # Entry: only if regime is directional and momentum agrees
                if regime.regime in ("STRONG_BULL_TREND", "WEAK_BULL_TREND") and mom > 0.01:
                    cost = price * (1 + fee + slip)
                    risk_capital = equity * self.config.risk_fraction
                    position = risk_capital / cost if cost > 0 else 0
                    entry_price = cost
                    entry_bar = i
                    entry_regime = regime.regime
                elif regime.regime in ("STRONG_BEAR_TREND", "WEAK_BEAR_TREND") and mom < -0.01:
                    cost = price * (1 - fee - slip)
                    risk_capital = equity * self.config.risk_fraction
                    position = -(risk_capital / cost) if cost > 0 else 0
                    entry_price = cost
                    entry_bar = i
                    entry_regime = regime.regime
            else:
                # Exit: momentum fades or opposite regime
                exit_signal = False
                if position > 0 and (mom < 0 or "BEAR" in regime.regime):
                    exit_signal = True
                if position < 0 and (mom > 0 or "BULL" in regime.regime):
                    exit_signal = True
                # Force exit near end
                if i == n - 1:
                    exit_signal = True

                if exit_signal and position != 0:
                    if position > 0:
                        exit_px = price * (1 - fee - slip)
                        pnl = (exit_px - entry_price) * position
                    else:
                        exit_px = price * (1 + fee + slip)
                        pnl = (entry_price - exit_px) * abs(position)
                    equity += pnl
                    returns.append(pnl / self.config.initial_capital)
                    trades.append(BacktestTrade(
                        bar_index=i,
                        symbol=symbol,
                        action="BUY" if position > 0 else "SELL",
                        entry_price=entry_price,
                        exit_price=exit_px,
                        pnl=pnl,
                        regime=entry_regime,
                        reason="momentum/regime exit",
                    ))
                    position = 0.0

            peak = max(peak, equity)
            dd = (peak - equity) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)

        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]
        gross_win = sum(t.pnl for t in wins) if wins else 0.0
        gross_loss = abs(sum(t.pnl for t in losses)) if losses else 0.0
        pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
        win_rate = len(wins) / len(trades) if trades else 0.0

        # Sharpe proxy: mean return / std of per-trade returns
        sharpe = 0.0
        if len(returns) > 1:
            mean_r = sum(returns) / len(returns)
            var = sum((r - mean_r) ** 2 for r in returns) / len(returns)
            std = var ** 0.5
            sharpe = (mean_r / std) if std > 0 else 0.0

        return BacktestResult(
            strategy_id=strategy.id,
            strategy_version=strategy.version,
            symbol=symbol,
            bars=n,
            trades=tuple(trades),
            total_pnl=equity - self.config.initial_capital,
            win_rate=round(win_rate, 4),
            profit_factor=round(pf if pf != float("inf") else 999.0, 4),
            max_drawdown=round(max_dd, 4),
            sharpe_proxy=round(sharpe, 4),
            final_equity=round(equity, 2),
            meta={
                "fee_pct": self.config.fee_pct,
                "slippage_pct": self.config.slippage_pct,
                "trade_count": len(trades),
            },
        )


__all__ = ["BacktestConfig", "BacktestTrade", "BacktestResult", "Backtester"]
