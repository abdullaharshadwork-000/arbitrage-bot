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

After a trade (or decision):

```
Experience → ReflectionAgent → Hypothesis → Experiment → Backtester → PromotionEngine
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
| `backtest.py` | 11 | Research-only backtest / evaluation harness |
| `promotion.py` | 16–17 | Champion/Challenger + Promotion (default HUMAN_APPROVAL) |
| `orchestrator.py` | 20 | Wires observe → select → propose → critique |

## Hard safety boundaries (unchanged)

* RiskManager is still the final pre-trade gate.
* Real orders still require:
  * `mode == live`
  * `execution_mode == real`
  * exact acknowledgement string `I ACCEPT REAL LOSSES`
  * complete credentials
* Agents never call Binance / ccxt directly.
* Agents never raise hard risk limits.
* Orchestrator currently emits `NO_TRADE` proposals until a concrete signal
  engine is deliberately wired.
* Promotion defaults to human approval; auto-promote only reaches LIVE_CANARY.
* Backtester is research-only and never touches live execution.

## What is deliberately NOT done yet

* Walk-forward and stress-test harnesses
* Shadow & persistent paper portfolio for challengers
* Live canary capital allocation
* ML / RL models
* Dashboard Strategy Lab / Research Lab UI
* Automatic wiring of the orchestrator into the main scan loop

These come next and must remain behind the same safety gates.

## Running the existing bot

Unchanged:

```bash
pip install -r requirements.txt
python server.py
```

All original paper / real / risk behaviour continues to work.
