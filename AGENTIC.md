# ArbiCore Agentic Foundation

This document describes the agentic / self-improving layers added on top of the
original arbitrage engine. **Nothing here replaces or weakens the existing Risk
Kernel, LiveModeGuard, or real-trading gates.**

## Pipeline (current)

```
Market prices
    → FeatureEngine          (timestamp-safe features)
    → RegimeDetector         (deterministic regime + confidence)
    → StrategySelector       (APPROVED strategies or NO_TRADE)
    → TradeProposal          (structured only – never raw NL)
    → CriticAgent            (APPROVE / WARN / REJECT)
    → [existing RiskManager] (final authority – unchanged)
    → [existing Execution]   (only if LiveModeGuard allows)
```

Research / improvement loop:

```
Experience → Reflection → Hypothesis → Experiment
    → Backtester → WalkForward → Stress → Shadow → Promotion (human by default)
```

## Modules added

| Module | Phase | Role |
|--------|-------|------|
| `domain.py` | 1 | OperatingMode, StrategyVersion, AuditEvent, Experience, TradeProposal |
| `guards.py` | 1 | LiveModeGuard – LIVE cannot be enabled silently |
| `memory.py` | 2 | Experience + rich audit persistence |
| `features.py` | 3 | Unified FeatureEngine |
| `regime.py` | 4 | RegimeDetector |
| `strategy_registry.py` | 5 | Versioned strategies + genealogy |
| `selection.py` | 6 | StrategySelector (NO_TRADE is valid) |
| `critic.py` | 7 | CriticAgent |
| `reflection.py` | 8 | ReflectionAgent (decision quality ≠ outcome) |
| `research.py` | 9–10 | Hypothesis + Experiment lab |
| `backtest.py` | 11 | Research-only backtest harness |
| `validation.py` | 12–13 | Walk-forward + stress checks |
| `shadow.py` | 14–15 | Shadow portfolio for challengers |
| `promotion.py` | 16–17 | Champion/Challenger + Promotion (default HUMAN_APPROVAL) |
| `orchestrator.py` | 20 | Wires observe → select → propose → critique |

## Hard safety boundaries (unchanged)

* RiskManager is still the final pre-trade gate.
* Real orders still require mode=live, execution_mode=real, exact ack string, complete credentials.
* Agents never call the exchange directly.
* Agents never raise hard risk limits.
* Orchestrator currently emits NO_TRADE until a concrete signal engine is wired.
* Promotion defaults to human approval; auto-promote only reaches LIVE_CANARY.
* Backtest / walk-forward / stress / shadow are research-only.

## What is deliberately NOT done yet

* Automatic wiring of the orchestrator into the main scan loop
* Dashboard Strategy Lab / Research Lab UI
* ML / RL models
* Live canary capital allocation with real size limits

## Running the existing bot

Unchanged:

```bash
pip install -r requirements.txt
python server.py
```
