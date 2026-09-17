"""Optional agent observation loop.

Phases 21 + 25.

When enabled (ARBICORE_AGENT_LOOP=1), runs:
  Features → Regime → Selection → Signal → Critic
  → optional PaperExecutor (only on Critic APPROVE)
  → optional ExperienceMemory record

NEVER submits live orders. NEVER bypasses LiveModeGuard / RiskManager.
Default: disabled.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional, Sequence
from uuid import uuid4

from .domain import Experience, OperatingMode
from .memory import ExperienceMemory
from .orchestrator import AgentOrchestrator, PipelineResult
from .paper_exec import PaperExecutor, PaperExecResult
from .strategy_registry import StrategyRegistry


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def agent_loop_enabled(environ: Optional[dict] = None) -> bool:
    """Feature flag. Default off. Set ARBICORE_AGENT_LOOP=1 to enable."""
    env = environ if environ is not None else os.environ
    return str(env.get("ARBICORE_AGENT_LOOP", "0")).strip().lower() in {
        "1", "true", "yes", "on",
    }


def paper_exec_enabled(environ: Optional[dict] = None) -> bool:
    """Separate flag for paper fills inside the observation loop. Default off."""
    env = environ if environ is not None else os.environ
    return str(env.get("ARBICORE_AGENT_PAPER_EXEC", "0")).strip().lower() in {
        "1", "true", "yes", "on",
    }


@dataclass
class AgentLoopState:
    enabled: bool = False
    paper_exec_enabled: bool = False
    cycles: int = 0
    paper_fills: int = 0
    last_result: Optional[PipelineResult] = None
    last_paper: Optional[PaperExecResult] = None
    last_error: str = ""
    last_run_at: Optional[datetime] = None
    history: list = field(default_factory=list)
    max_history: int = 50

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "paper_exec_enabled": self.paper_exec_enabled,
            "cycles": self.cycles,
            "paper_fills": self.paper_fills,
            "last_error": self.last_error,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_action": (
                self.last_result.proposal.action
                if self.last_result and self.last_result.proposal
                else None
            ),
            "last_critique": (
                self.last_result.critique.result
                if self.last_result and self.last_result.critique
                else None
            ),
            "last_regime": (
                self.last_result.regime.regime
                if self.last_result and self.last_result.regime
                else None
            ),
            "last_paper_accepted": (
                self.last_paper.accepted if self.last_paper else None
            ),
            "history_len": len(self.history),
        }


class AgentObservationLoop:
    """Observation (+ optional paper execution) cycle."""

    def __init__(
        self,
        registry: Optional[StrategyRegistry] = None,
        *,
        memory: Optional[ExperienceMemory] = None,
        paper_executor: Optional[PaperExecutor] = None,
        mode: OperatingMode = OperatingMode.PAPER,
        enabled: Optional[bool] = None,
        enable_paper_exec: Optional[bool] = None,
    ):
        if mode is OperatingMode.LIVE:
            raise ValueError("AgentObservationLoop cannot run in LIVE mode")
        self.registry = registry or StrategyRegistry()
        self.memory = memory
        self.orchestrator = AgentOrchestrator(self.registry, mode=mode)
        self.paper = paper_executor
        self.state = AgentLoopState(
            enabled=agent_loop_enabled() if enabled is None else bool(enabled),
            paper_exec_enabled=(
                paper_exec_enabled()
                if enable_paper_exec is None
                else bool(enable_paper_exec)
            ),
        )

    def enable(self) -> None:
        self.state.enabled = True

    def disable(self) -> None:
        self.state.enabled = False

    def run_once(
        self,
        symbol: str,
        prices: Sequence[float],
        *,
        volumes: Optional[Sequence[float]] = None,
    ) -> Optional[PipelineResult]:
        if not self.state.enabled:
            return None
        try:
            result = self.orchestrator.run_cycle(symbol, prices, volumes=volumes)
            self.state.cycles += 1
            self.state.last_result = result
            self.state.last_error = ""
            self.state.last_run_at = _utc_now()
            self.state.last_paper = None

            paper_info: dict[str, Any] = {}
            if (
                self.state.paper_exec_enabled
                and self.paper is not None
                and result.proposal
                and result.critique
            ):
                last_price = float(prices[-1]) if prices else 0.0
                paper_result = self.paper.execute(
                    result.proposal, result.critique, last_price=last_price
                )
                self.state.last_paper = paper_result
                paper_info = {
                    "paper_accepted": paper_result.accepted,
                    "paper_reason": paper_result.reject_reason,
                }
                if paper_result.accepted and paper_result.fill:
                    self.state.paper_fills += 1
                    paper_info["fill_id"] = paper_result.fill.id
                    self._record_experience(symbol, result, paper_result)

            summary = {
                "at": self.state.last_run_at.isoformat(),
                "symbol": symbol,
                "regime": result.regime.regime if result.regime else None,
                "selection": result.selection.action if result.selection else None,
                "proposal": result.proposal.action if result.proposal else None,
                "critique": result.critique.result if result.critique else None,
                **paper_info,
            }
            self.state.history.append(summary)
            if len(self.state.history) > self.state.max_history:
                self.state.history = self.state.history[-self.state.max_history :]

            if self.memory and result.critique:
                from .domain import AuditCategory, AuditEvent

                event = AuditEvent.create(
                    AuditCategory.SYSTEM,
                    "agent_observation",
                    component="AgentObservationLoop",
                    symbol=symbol,
                    mode=self.orchestrator.mode,
                    payload=summary,
                    reason="observation cycle",
                )
                self.memory.record_audit_event(event)

            return result
        except Exception as exc:
            self.state.last_error = str(exc)
            self.state.last_run_at = _utc_now()
            return None

    def _record_experience(
        self,
        symbol: str,
        result: PipelineResult,
        paper_result: PaperExecResult,
    ) -> None:
        if not self.memory or not result.proposal or not paper_result.fill:
            return
        fill = paper_result.fill
        exp = Experience(
            id=f"exp_{uuid4().hex[:12]}",
            timestamp=_utc_now(),
            symbol=symbol,
            mode=self.orchestrator.mode,
            strategy_id=result.proposal.strategy_id,
            strategy_version=result.proposal.strategy_version,
            regime=result.regime.regime if result.regime else None,
            regime_confidence=result.regime.confidence if result.regime else None,
            proposed_action=result.proposal.action,
            final_action=fill.action,
            trade_confidence=result.proposal.trade_confidence,
            requested_risk_fraction=result.proposal.requested_risk_fraction,
            entry_price=result.proposal.entry_price,
            actual_entry=Decimal(str(fill.fill_price)),
            stop_loss=result.proposal.stop_loss,
            take_profit=result.proposal.take_profit,
            position_size=Decimal(str(fill.quantity)),
            fees=Decimal(str(fill.fee)),
            critic_result=result.critique.result if result.critique else None,
            risk_result="paper_adapter",
            feature_snapshot=result.features.features if result.features else {},
        )
        self.memory.record_experience(exp)


__all__ = [
    "agent_loop_enabled",
    "paper_exec_enabled",
    "AgentLoopState",
    "AgentObservationLoop",
]
