"""ArbiCore — production support modules for the arbitrage engine.

`arbitrage_bot.py` remains the engine entry point and keeps its public API
so the Flask dashboard and existing tests keep working.

Agentic foundation (Phases 1–23+) never bypasses the Risk Kernel or LiveModeGuard.
"""

__all__ = [
    "agent_api",
    "agent_loop",
    "alerts",
    "auth",
    "backtest",
    "books",
    "config",
    "critic",
    "domain",
    "features",
    "feed",
    "guards",
    "ledger",
    "memory",
    "money",
    "orders",
    "orchestrator",
    "pipeline",
    "promotion",
    "rebalance",
    "reconcile",
    "reflection",
    "regime",
    "research",
    "risk",
    "safety",
    "selection",
    "shadow",
    "signals_agent",
    "simulator",
    "strategy_registry",
    "streaming",
    "validation",
]
