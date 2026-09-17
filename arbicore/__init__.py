"""ArbiCore — production support modules for the arbitrage engine.

`arbitrage_bot.py` remains the engine entry point and keeps its public API
(PaperWallet, DemoFeed, LiveFeed, find_opportunity, ...) so the Flask
dashboard and the existing test suite keep working. The hard parts that
touch real money live here, in small testable units:

    money        Decimal arithmetic and exchange step quantization
    config       immutable runtime config + credential loading from env
    books        order-book VWAP and depth analysis
    feed         bulk quote collection, staleness checks, route ranking
    orders       order submission with clientOrderId + fill reconciliation
    ledger       shared per-(exchange, currency) balances
    simulator    latency- and depth-aware paper fills
    rebalance    inventory transfers with network fees and confirmation delay
    risk         kill switches and circuit breakers
    reconcile    restart-time state recovery against the exchange
    alerts       outbound notifications for unattended operation
    domain       foundational typed models for the agentic platform (Phase 1)
    guards       explicit live-mode / real-trading gates (Phase 1)
    memory       experience + rich audit persistence (Phase 2)
"""

__all__ = [
    "alerts",
    "auth",
    "books",
    "config",
    "domain",
    "feed",
    "guards",
    "ledger",
    "memory",
    "money",
    "orders",
    "rebalance",
    "reconcile",
    "safety",
    "risk",
    "simulator",
    "streaming",
]
