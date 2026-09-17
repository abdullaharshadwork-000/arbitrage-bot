"""ArbiCore — production support modules for the arbitrage engine.

`arbitrage_bot.py` remains the engine entry point and keeps its public API
(PaperWallet, DemoFeed, LiveFeed, find_opportunity, ...) so the Flask
dashboard and the existing test suite keep working. The hard parts that
touch real money live here, in small testable units.

Agentic foundation (Phases 1–21+) lives alongside the original modules and
never bypasses the Risk Kernel or LiveModeGuard.
"""

__all__ = [
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
    "simulator",
    "strategy_registry",
    "streaming",
    "validation",
]
