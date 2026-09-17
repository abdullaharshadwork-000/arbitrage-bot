"""Agent Orchestrator – wires the decision pipeline.

Phase 20 foundation.

Pipeline:
  Features → Regime → Selection → TradeProposal → Critic

This orchestrator NEVER calls the exchange and NEVER bypasses the Risk Kernel.
It only produces structured proposals and critiques that a later execution
layer (still gated by RiskManager + LiveModeGuard) may act on.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional, Sequence
from uuid import uuid4

from .critic import CriticAgent, CriticDecision
from .domain import OperatingMode, TradeProposal
from .features import FeatureEngine, FeatureSnapshot
from .regime import RegimeDecision, RegimeDetector
from .selection import SelectionResult, StrategySelector
from .strategy_registry import StrategyRegistry


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class PipelineResult:
    """Full output of one orchestrator cycle."""

    features: Optional[FeatureSnapshot]
    regime: Optional[RegimeDecision]
    selection: Optional[SelectionResult]
    proposal: Optional[TradeProposal]
    critique: Optional[CriticDecision]
    timestamp: datetime = field(default_factory=_utc_now)
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "features": self.features.as_dict() if self.features else None,
            "regime": self.regime.as_dict() if self.regime else None,
            "selection": self.selection.as_dict() if self.selection else None,
            "proposal": self.proposal.as_dict() if self.proposal else None,
            "critique": self.critique.as_dict() if self.critique else None,
            "timestamp": self.timestamp.isoformat(),
            "meta": dict(self.meta),
        }


class AgentOrchestrator:
    """Run one observe → understand → select → propose → critique cycle."""

    def __init(
        self,
        registry: StrategyRegistry,
        *,
        feature_engine: Optional[FeatureEngine] = None,
        regime_detector: Optional[RegimeDetector] = None,
        selector: Optional[StrategySelector] = None,
        critic: Optional[CriticAgent] = None,
        mode: OperatingMode = OperatingMode.PAPER,
    ):
        self.registry = registry
        self.features = feature_engine or FeatureEngine()
        self.regime = regime_detector or RegimeDetector()
        self.selector = selector or StrategySelector()
        self.critic = critic or CriticAgent()
        self.mode = mode

    def run_cycle(
        self,
        symbol: str,
        prices: Sequence[float],
        *,
        volumes: Optional[Sequence[float]] = None,
        as_of: Optional[float] = None,
    ) -> PipelineResult:
        # 1. Features
        snap = self.features.compute(symbol, prices, as_of=as_of, volumes=volumes)

        # 2. Regime
        regime = self.regime.classify(snap)

        # 3. Selection
        candidates = self.registry.all_versions()
        selection = self.selector.select(candidates, regime, symbol=symbol)

        # 4. Proposal (very simple – later phases will enrich)
        proposal: Optional[TradeProposal] = None
        if selection.action == "USE_STRATEGY" and selection.strategy_id:
            proposal = TradeProposal(
                id=f"prop_{uuid4().hex[:12]}",
                symbol=symbol,
                action="NO_TRADE",  # safe default until a real signal engine is wired
                strategy_id=selection.strategy_id,
                strategy_version=selection.strategy_version or "",
                market_regime=regime.regime,
                regime_confidence=regime.confidence,
                trade_confidence=0.0,
                reasoning_summary=(
                    f"Selected {selection.strategy_name} for regime {regime.regime}; "
                    "awaiting concrete entry signal (orchestrator does not invent entries)"
                ),
            )
        else:
            proposal = TradeProposal(
                id=f"prop_{uuid4().hex[:12]}",
                symbol=symbol,
                action="NO_TRADE",
                strategy_id="none",
                strategy_version="none",
                market_regime=regime.regime,
                regime_confidence=regime.confidence,
                trade_confidence=1.0,
                reasoning_summary=selection.reason or "NO_TRADE",
            )

        # 5. Critique
        critique = self.critic.review(proposal, features=snap, regime=regime)

        return PipelineResult(
            features=snap,
            regime=regime,
            selection=selection,
            proposal=proposal,
            critique=critique,
            meta={"mode": self.mode.value, "symbol": symbol},
        )


__all__ = ["PipelineResult", "AgentOrchestrator"]
