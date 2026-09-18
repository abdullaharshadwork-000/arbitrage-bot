"""End-to-end decision pipeline for agent proposals.

Critic → OrderIntentBridge → RiskAdapter → Handoff → optional LiveExecutor

Live exchange calls only happen when:
* LiveExecutor is injected
* ARBICORE_AGENT_LIVE_EXEC=1
* LiveModeGuard allows real orders
* RiskManager already approved the notional
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .critic import CriticAgent, CriticDecision
from .domain import OperatingMode, TradeProposal
from .execution_handoff import handoff
from .guards import LiveModeGuard
from .live_bridge import BridgeResult, OrderIntentBridge
from .live_exec import LiveExecutor, LiveExecResult, live_exec_enabled
from .risk import RiskManager
from .risk_adapter import RiskAdapter, RiskAdapterResult


@dataclass(frozen=True)
class DecisionPipelineResult:
    proposal: TradeProposal
    critique: CriticDecision
    bridge: BridgeResult
    risk: Optional[RiskAdapterResult] = None
    handoff_entry: Optional[dict[str, Any]] = None
    live: Optional[LiveExecResult] = None

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
            "handoff": self.handoff_entry,
            "live": self.live.as_dict() if self.live else None,
            "approved": self.approved,
        }


class DecisionPipeline:
    def __init__(
        self,
        risk_manager: RiskManager,
        *,
        critic: Optional[CriticAgent] = None,
        bridge: Optional[OrderIntentBridge] = None,
        risk_adapter: Optional[RiskAdapter] = None,
        live_guard: Optional[LiveModeGuard] = None,
        live_executor: Optional[LiveExecutor] = None,
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
        self.live_executor = live_executor
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
        handoff_entry = None
        live_result = None
        if bridge.accepted and bridge.intent is not None:
            risk_result = self.risk_adapter.evaluate(bridge.intent)
            if risk_result.allowed and risk_result.request is not None:
                handoff_entry = handoff(risk_result.request)
                if (
                    self.live_executor is not None
                    and live_exec_enabled()
                    and self.mode is OperatingMode.LIVE
                ):
                    live_result = self.live_executor.execute(risk_result.request)
        return DecisionPipelineResult(
            proposal=proposal,
            critique=critique,
            bridge=bridge,
            risk=risk_result,
            handoff_entry=handoff_entry,
            live=live_result,
        )


__all__ = ["DecisionPipelineResult", "DecisionPipeline"]
