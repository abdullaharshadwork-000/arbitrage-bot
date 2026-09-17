"""Scan-loop hook for the agent observation path.

Phase 27.

Intended use: from the existing scan worker, after quotes are collected,
optionally call `notify_agent_prices(symbol, price_history)`.

* Respects ARBICORE_AGENT_LOOP / ARBICORE_AGENT_PAPER_EXEC flags
* Never places live orders
* Failures are swallowed so the main scanner never breaks
"""

from __future__ import annotations

from typing import Optional, Sequence

from .agent_loop import AgentObservationLoop, agent_loop_enabled
from .domain import OperatingMode
from .paper_exec import PaperExecutor, paper_exec_enabled  # type: ignore
from .strategy_registry import StrategyRegistry

# Lazy singleton – created on first use when the flag is on
_loop: Optional[AgentObservationLoop] = None


def get_agent_loop() -> Optional[AgentObservationLoop]:
    """Return the process-wide observation loop, or None if disabled."""
    global _loop
    if not agent_loop_enabled():
        return None
    if _loop is None:
        registry = StrategyRegistry()
        paper = None
        try:
            from .agent_loop import paper_exec_enabled as _pe

            if _pe():
                paper = PaperExecutor(mode=OperatingMode.PAPER)
        except Exception:
            paper = None
        _loop = AgentObservationLoop(
            registry=registry,
            paper_executor=paper,
            mode=OperatingMode.PAPER,
            enabled=True,
            enable_paper_exec=paper is not None,
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


def agent_snapshot() -> dict:
    """Read-only snapshot for APIs / debugging."""
    loop = _loop
    if loop is None:
        return {"enabled": agent_loop_enabled(), "attached": False}
    snap = loop.state.snapshot()
    snap["attached"] = True
    return snap


__all__ = ["get_agent_loop", "notify_agent_prices", "agent_snapshot"]
