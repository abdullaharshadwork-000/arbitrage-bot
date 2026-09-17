"""Shadow / Paper portfolio for challenger strategies.

Phases 14–15 foundation.

Runs a strategy against live (or recorded) prices without sending orders.
Tracks hypothetical equity, trades, and metrics so a challenger can be
compared to the champion before any real capital is allocated.

Never places real orders. Never changes Risk Kernel limits.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Sequence
from uuid import uuid4

from .domain import StrategyVersion
from .features import FeatureEngine, FeatureSnapshot
from .regime import RegimeDecision, RegimeDetector


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ShadowTrade:
    id: str
    symbol: str
    action: str
    entry_price: float
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    regime: str = ""
    opened_at: datetime = field(default_factory=_utc_now)
    closed_at: Optional[datetime] = None

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["opened_at"] = self.opened_at.isoformat()
        if self.closed_at:
            data["closed_at"] = self.closed_at.isoformat()
        return data


@dataclass
class ShadowPortfolio:
    strategy_id: str
    strategy_version: str
    initial_capital: float = 10_000.0
    equity: float = 10_000.0
    peak_equity: float = 10_000.0
    max_drawdown: float = 0.0
    open_position: Optional[ShadowTrade] = None
    closed_trades: list = field(default_factory=list)
    fee_pct: float = 0.10
    slippage_pct: float = 0.05
    risk_fraction: float = 0.01

    def snapshot(self) -> dict[str, Any]:
        wins = [t for t in self.closed_trades if (t.pnl or 0) > 0]
        return {
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "equity": round(self.equity, 2),
            "max_drawdown": round(self.max_drawdown, 4),
            "trade_count": len(self.closed_trades),
            "open": self.open_position is not None,
            "win_rate": (len(wins) / len(self.closed_trades)) if self.closed_trades else 0.0,
            "total_pnl": round(self.equity - self.initial_capital, 2),
        }


class ShadowRunner:
    """Update a ShadowPortfolio from the latest price / feature / regime."""

    def __init(
        self,
        portfolio: ShadowPortfolio,
        *,
        feature_engine: Optional[FeatureEngine] = None,
        regime_detector: Optional[RegimeDetector] = None,
    ):
        self.portfolio = portfolio
        self.features = feature_engine or FeatureEngine()
        self.regime = regime_detector or RegimeDetector()

    def on_bar(
        self,
        symbol: str,
        prices: Sequence[float],
        *,
        volumes: Optional[Sequence[float]] = None,
    ) -> dict[str, Any]:
        """Process one new bar. Returns a small event dict."""
        if len(prices) < 10:
            return {"event": "skip", "reason": "insufficient_bars"}

        snap = self.features.compute(symbol, prices, volumes=volumes)
        regime = self.regime.classify(snap)
        price = float(prices[-1])
        mom = snap.get("momentum_10", 0.0)
        fee = self.portfolio.fee_pct / 100.0
        slip = self.portfolio.slippage_pct / 100.0
        p = self.portfolio

        event: dict[str, Any] = {"event": "hold", "price": price, "regime": regime.regime}

        if p.open_position is None:
            # Entry rules (same transparent placeholder as backtester)
            action = None
            if regime.regime in ("STRONG_BULL_TREND", "WEAK_BULL_TREND") and mom > 0.01:
                action = "BUY"
            elif regime.regime in ("STRONG_BEAR_TREND", "WEAK_BEAR_TREND") and mom < -0.01:
                action = "SELL"

            if action:
                entry = price * (1 + fee + slip) if action == "BUY" else price * (1 - fee - slip)
                p.open_position = ShadowTrade(
                    id=f"sh_{uuid4().hex[:10]}",
                    symbol=symbol,
                    action=action,
                    entry_price=entry,
                    regime=regime.regime,
                )
                event = {"event": "open", "action": action, "price": entry, "regime": regime.regime}
        else:
            pos = p.open_position
            exit_signal = False
            if pos.action == "BUY" and (mom < 0 or "BEAR" in regime.regime):
                exit_signal = True
            if pos.action == "SELL" and (mom > 0 or "BULL" in regime.regime):
                exit_signal = True

            if exit_signal:
                if pos.action == "BUY":
                    exit_px = price * (1 - fee - slip)
                    size = (p.equity * p.risk_fraction) / pos.entry_price
                    pnl = (exit_px - pos.entry_price) * size
                else:
                    exit_px = price * (1 + fee + slip)
                    size = (p.equity * p.risk_fraction) / pos.entry_price
                    pnl = (pos.entry_price - exit_px) * size

                pos.exit_price = exit_px
                pos.pnl = pnl
                pos.closed_at = _utc_now()
                p.equity += pnl
                p.peak_equity = max(p.peak_equity, p.equity)
                dd = (p.peak_equity - p.equity) / p.peak_equity if p.peak_equity > 0 else 0
                p.max_drawdown = max(p.max_drawdown, dd)
                p.closed_trades.append(pos)
                p.open_position = None
                event = {"event": "close", "pnl": pnl, "equity": p.equity}

        return event


__all__ = ["ShadowTrade", "ShadowPortfolio", "ShadowRunner"]
