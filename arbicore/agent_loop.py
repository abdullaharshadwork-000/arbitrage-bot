"""Optional agent observation loop.

Phase 21.

When enabled (via settings or explicit flag), runs the AgentOrchestrator on
each price update and records the PipelineResult. It NEVER submits orders
and NEVER bypasses RiskManager / LiveModeGuard.

Default: disabled. Existing scan/execution behaviour is unchanged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from .domain import OperatingMode
from .memory import ExperienceMemory
from .orchestrator import AgentOrchestrator, PipelineResult
from .strategy_registry import StrategyRegistry


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def agent_loop_enabled(environ: Optional[dict] = None) -> bool:
    """Feature flag. Default off. Set ARBICORE_AGENT_LOOP=1 to enable."""
    env = environ if environ is not None else os.environ
    return str(env.get("ARBICORE_AGENT_LOOP", "0")).strip().lower() in {
        "1", "true", "yes", "on",
    }


@dataclass
class AgentLoopState:
    """In-process state for the observation loop."""

    enabled: bool = False
    cycles: int = 0
    last_result: Optional[PipelineResult] = None
    last_error: str = ""
    last_run_at: Optional[datetime] = None
    history: list = field(default_factory=list)
    max_history: int = 50

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "cycles": self.cycles,
            "last_error": self.last_error,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_action": (
                self.last_result.proposal.action
                if self.last_result and self.last_result.proposal
                else None
            ),
            "last_regime": (
                self.last_result.regime.regime
                if self.last_result and self.last_result.regime
                else None
            ),
            "history_len": len(self.history),
        }


class AgentObservationLoop:
    """Run orchestrator cycles without execution.

    Safe to call from the main scan loop when the feature flag is on.
    All proposals remain NO_TRADE until a later phase deliberately
    wires a signal engine and execution path (still behind Risk Kernel).
    """

    def __init__(
        self,
        registry: Optional[StrategyRegistry] = None,
        *,
        memory: Optional[ExperienceMemory] = None,
        mode: OperatingMode = OperatingMode.PAPER,
        enabled: Optional[bool] = None,
    ):
        self.registry = registry or StrategyRegistry()
        self.memory = memory
        self.orchestrator = AgentOrchestrator(self.registry, mode=mode)
        self.state = AgentLoopState(
            enabled=agent_loop_enabled() if enabled is None else bool(enabled)
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
        """One observation cycle. Returns None if disabled or on error."""
        if not self.state.enabled:
            return None
        try:
            result = self.orchestrator.run_cycle(
                symbol, prices, volumes=volumes
            )
            self.state.cycles += 1
            self.state.last_result = result
            self.state.last_error = ""
            self.state.last_run_at = _utc_now()

            summary = {
                "at": self.state.last_run_at.isoformat(),
                "symbol": symbol,
                "regime": result.regime.regime if result.regime else None,
                "selection": result.selection.action if result.selection else None,
                "proposal": result.proposal.action if result.proposal else None,
                "critique": result.critique.result if result.critique else None,
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
                    reason="observation cycle (no execution)",
                )
                self.memory.record_audit_event(event)

            return result
        except Exception as exc:
            self.state.last_error = str(exc)
            self.state.last_run_at = _utc_now()
            return None


__all__ = [
    "agent_loop_enabled",
    "AgentLoopState",
    "AgentObservationLoop",
]
