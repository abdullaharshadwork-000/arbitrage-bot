"""Scan-loop hook for the agent observation path.

Phases 27 + 30.

From the existing scan worker, after mid prices are known:

    from arbicore.scan_hook import notify_agent_mid
    notify_agent_mid("BTC/USDT", mid_price)

* Respects ARBICORE_AGENT_LOOP / ARBICORE_AGENT_PAPER_EXEC
* Keeps rolling price history per symbol
* Attaches ExperienceMemory (agent experiences + audit)
* Never places live orders; never raises into the scanner
"""

from __future__ import annotations

import os
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Optional, Sequence

from .agent_loop import (
    AgentObservationLoop,
    agent_loop_enabled,
    paper_exec_enabled,
)
from .domain import OperatingMode
from .memory import ExperienceMemory
from .paper_exec import PaperExecutor
from .strategy_registry import StrategyRegistry

_loop: Optional[AgentObservationLoop] = None
_memory: Optional[ExperienceMemory] = None
_price_buffers: dict[str, deque] = defaultdict(lambda: deque(maxlen=120))
_MIN_BARS = 15


def _default_memory_path() -> Path:
    override = os.environ.get("ARBICORE_AGENT_DB")
    if override:
        return Path(override)
    # Prefer same directory as main ARBICORE_DB when set
    main_db = os.environ.get("ARBICORE_DB")
    if main_db:
        return Path(main_db).with_name("arbicore_agent.db")
    return Path.cwd() / "arbicore_agent.db"


def get_memory() -> ExperienceMemory:
    global _memory
    if _memory is None:
        _memory = ExperienceMemory(_default_memory_path())
    return _memory


def get_agent_loop() -> Optional[AgentObservationLoop]:
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
        try:
            memory = get_memory()
        except Exception:
            memory = None
        _loop = AgentObservationLoop(
            registry=registry,
            memory=memory,
            paper_executor=paper,
            mode=OperatingMode.PAPER,
            enabled=True,
            enable_paper_exec=enable_paper,
            seed_demo=True,
        )
    return _loop


def notify_agent_mid(symbol: str, mid_price: float) -> None:
    """Push one mid price; runs a cycle when enough history exists."""
    try:
        px = float(mid_price)
        if px <= 0 or not symbol:
            return
        buf = _price_buffers[str(symbol)]
        buf.append(px)
        if len(buf) < _MIN_BARS:
            return
        loop = get_agent_loop()
        if loop is None:
            return
        loop.run_once(str(symbol), list(buf))
    except Exception:
        return


def notify_agent_prices(
    symbol: str,
    prices: Sequence[float],
    *,
    volumes: Optional[Sequence[float]] = None,
) -> None:
    """Push a full price series (also updates the rolling buffer)."""
    try:
        clean = [float(p) for p in prices if float(p) > 0]
        if len(clean) < _MIN_BARS:
            return
        buf = _price_buffers[str(symbol)]
        buf.clear()
        for p in clean[-120:]:
            buf.append(p)
        loop = get_agent_loop()
        if loop is None:
            return
        loop.run_once(str(symbol), list(buf), volumes=volumes)
    except Exception:
        return


def agent_snapshot() -> dict[str, Any]:
    mem_info: dict[str, Any] = {}
    try:
        mem = get_memory()
        mem_info = {
            "db": str(mem.db_path),
            "experience_count": mem.count_experiences(),
        }
    except Exception:
        mem_info = {"db": None, "experience_count": 0}

    if _loop is None:
        return {
            "enabled": agent_loop_enabled(),
            "attached": False,
            "buffered_symbols": list(_price_buffers.keys()),
            "memory": mem_info,
        }
    snap = _loop.state.snapshot()
    snap["attached"] = True
    snap["buffered_symbols"] = {
        sym: len(buf) for sym, buf in _price_buffers.items()
    }
    snap["memory"] = mem_info
    return snap


__all__ = [
    "get_agent_loop",
    "get_memory",
    "notify_agent_mid",
    "notify_agent_prices",
    "agent_snapshot",
]
