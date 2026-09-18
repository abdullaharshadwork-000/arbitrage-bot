"""End-to-end decision pipeline for agent proposals.

Critic → OrderIntentBridge → RiskAdapter

Never calls Binance. Produces either an ApprovedOrderRequest or a rejection.
Downstream existing execution may consume ApprovedOrderRequest only when
Settings.real and LiveModeGuard already allow it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .critic import CriticAgent, CriticDecision
from .domain import OperatingMode, TradeProposal
from .live_bridge import BridgeResult, OrderIntentBridge
from .risk import RiskManager
from .risk_adapter import RiskAdapter, RiskAdapterResult
from .guards import LiveModeGuard


@dataclass(frozen=True)
class DecisionPipelineResult:
    proposal: TradeProposal
    critique: CriticDecision
    bridge: BridgeResult
    risk: Optional[RiskAdapterResult] = None

    @property
    def approved(self) -> bool:
        return bool(
            self.risk is not None
            and self.risk.allowed
            and self.risk.request is not None
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal": self.proposal.as_dict(),
            "critique": self.critique.as_dict() if hasattr(self.critique, "as_dict") else {
                "result": self.critique.result,
                "confidence": getattr(self.critique, "confidence", None),
            },
            "bridge": self.bridge.as_dict(),
            "risk": self.risk.as_dict() if self.risk else None,
            "approved": self.approved,
        }


class DecisionPipeline:
    """Wire Critic + Bridge + RiskAdapter."""

    def __init__(
        self,
        risk_manager: RiskManager,
        *,
        critic: Optional[CriticAgent] = None,
        bridge: Optional[OrderIntentBridge] = None,
        risk_adapter: Optional[RiskAdapter] = None,
        live_guard: Optional[LiveModeGuard] = None,
        equity: float = 10_000.0,
        mode: OperatingMode = OperatingMode.PAPER,
    ):
        self.critic = critic or CriticAgent()
        self.bridge = bridge or OrderIntentBridge(mode=mode)
        self.risk_adapter = risk_adapter or RiskAdapter(
            risk_manager,
            live_guard=live_guard,
            equity=equity,
            mode=mode,
        )
        self.mode = mode

    def run(
        self,
        proposal: TradeProposal,
        *,
        features=None,
        regime=None,
    ) -> DecisionPipelineResult:
        critique = self.critic.review(proposal, features=features, regime=regime)
        bridge = self.bridge.build(proposal, critique)
        risk_result = None
        if bridge.accepted and bridge.intent is not None:
            risk_result = self.risk_adapter.evaluate(bridge.intent)
        return DecisionPipelineResult(
            proposal=proposal,
            critique=critique,
            bridge=bridge,
            risk=risk_result,
        )


__all__ = ["DecisionPipelineResult", "DecisionPipeline"]
