"""Bootstrap helpers for the agentic layer.

Phase 26.

* seed_demo_strategy(registry) – one APPROVED momentum strategy for demos
* Never enables LIVE or real trading
"""

from __future__ import annotations

from .domain import StrategyStatus, StrategyVersion
from .strategy_registry import StrategyRegistry


DEMO_STRATEGY_NAME = "momentum_regime_v1"


def seed_demo_strategy(registry: StrategyRegistry) -> StrategyVersion:
    """Ensure a single APPROVED demo strategy exists. Idempotent by name."""
    existing = registry.latest(DEMO_STRATEGY_NAME)
    if existing is not None:
        if existing.status is not StrategyStatus.APPROVED:
            return registry.set_status(existing.id, StrategyStatus.APPROVED)
        return existing

    sv = registry.create(
        name=DEMO_STRATEGY_NAME,
        version="1.0.0",
        status=StrategyStatus.APPROVED,
        description=(
            "Demo momentum + regime strategy for paper/observation only. "
            "Not authorized for live capital."
        ),
        parameters={
            "min_momentum": 0.012,
            "stop_pct": 0.008,
            "target_pct": 0.016,
            "risk_fraction": 0.005,
        },
        supported_symbols=("BTC/USDT", "ETH/USDT", "SOL/USDT"),
        intended_regimes=(
            "STRONG_BULL_TREND",
            "WEAK_BULL_TREND",
            "STRONG_BEAR_TREND",
            "WEAK_BEAR_TREND",
        ),
        feature_dependencies=("momentum_10", "realized_vol", "price"),
        creation_reason="bootstrap demo strategy",
        created_by="bootstrap",
    )
    return registry.set_status(sv.id, StrategyStatus.APPROVED)


__all__ = ["DEMO_STRATEGY_NAME", "seed_demo_strategy"]
