"""Scan-loop hook for the agent observation path.

Phase 27.

Intended use: from the existing scan worker, after quotes are collected,
optionally call `notify_agent_prices(symbol, price_history)`.

* Respects ARBICORE_AGENT_LOOP / ARBICORE_AGENT_PAPER_EXEC flags
* Never places live orders
* Failures are swallowed so the main scanner never breaks
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from .agent_loop import (
    AgentObservationLoop,
    agent_loop_enabled,
    paper_exec_enabled,
)
from .domain import OperatingMode
from .paper_exec import PaperExecutor
from .strategy_registry import StrategyRegistry

_loop: Optional[AgentObservationLoop] = None


def get_agent_loop() -> Optional[AgentObservationLoop]:
    """Return the process-wide observation loop, or None if disabled."""
    global _loop
    if not agent_loop_enabled():
        return None
    if _loop is None:
        registry = StrategyRegistry()
        paper = None
        enable_paper = False
        try:
            if paper_exec_enabled():
                paper = PaperExecutor(mode=OperatingMode.PAPER)
                enable_paper = True
        except Exception:
            paper = None
            enable_paper = False
        _loop = AgentObservationLoop(
            registry=registry,
            paper_executor=paper,
            mode=OperatingMode.PAPER,
            enabled=True,
            enable_paper_exec=enable_paper,
            seed_demo=True,
        )
    return _loop


def notify_agent_prices(
    symbol: str,
    prices: Sequence[float],
    *,
    volumes: Optional[Sequence[float]] = None,
) -> None:
    """Best-effort observation cycle. Never raises into the caller."""
    try:
        loop = get_agent_loop()
        if loop is None:
            return
        if not prices or len(prices) < 10:
            return
        loop.run_once(symbol, prices, volumes=volumes)
    except Exception:
        return


def agent_snapshot() -> dict[str, Any]:
    """Read-only snapshot for APIs / debugging."""
    if _loop is None:
        return {"enabled": agent_loop_enabled(), "attached": False}
    snap = _loop.state.snapshot()
    snap["attached"] = True
    return snap


__all__ = ["get_agent_loop", "notify_agent_prices", "agent_snapshot"]
