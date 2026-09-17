"""Agent Orchestrator – wires the decision pipeline.

Phases 20 + 23.

Pipeline:
  Features → Regime → Selection → SignalEngine → TradeProposal → Critic

This orchestrator NEVER calls the exchange and NEVER bypasses the Risk Kernel.
It only produces structured proposals and critiques that a later execution
layer (still gated by RiskManager + LiveModeGuard) may act on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from .critic import CriticAgent, CriticDecision
from .domain import OperatingMode, TradeProposal
from .features import FeatureEngine, FeatureSnapshot
from .regime import RegimeDecision, RegimeDetector
from .selection import SelectionResult, StrategySelector
from .signals_agent import SignalEngine
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
    """Run one observe → understand → select → signal → critique cycle."""

    def __init__(
        self,
        registry: StrategyRegistry,
        *,
        feature_engine: Optional[FeatureEngine] = None,
        regime_detector: Optional[RegimeDetector] = None,
        selector: Optional[StrategySelector] = None,
        signal_engine: Optional[SignalEngine] = None,
        critic: Optional[CriticAgent] = None,
        mode: OperatingMode = OperatingMode.PAPER,
    ):
        self.registry = registry
        self.features = feature_engine or FeatureEngine()
        self.regime = regime_detector or RegimeDetector()
        self.selector = selector or StrategySelector()
        self.signals = signal_engine or SignalEngine()
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
        snap = self.features.compute(symbol, prices, as_of=as_of, volumes=volumes)
        regime = self.regime.classify(snap)
        candidates = self.registry.all_versions()
        selection = self.selector.select(candidates, regime, symbol=symbol)

        last_price = float(prices[-1]) if prices else 0.0
        proposal = self.signals.propose(
            symbol, snap, regime, selection, last_price=last_price
        )

        critique = self.critic.review(proposal, features=snap, regime=regime)

        return PipelineResult(
            features=snap,
            regime=regime,
            selection=selection,
            proposal=proposal,
            critique=critique,
            meta={
                "mode": self.mode.value,
                "symbol": symbol,
                "proposal_action": proposal.action,
                "critique_result": critique.result,
            },
        )


__all__ = ["PipelineResult", "AgentOrchestrator"]
