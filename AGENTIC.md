# ArbiCore Agentic Foundation

Agentic / self-improving layers on top of the original arbitrage engine.
**Nothing here replaces or weakens the existing Risk Kernel, LiveModeGuard, or real-trading gates.**

## Live decision pipeline

```
Market prices
    → FeatureEngine
    → RegimeDetector
    → StrategySelector          (or NO_TRADE)
    → TradeProposal             (structured only)
    → CriticAgent               (APPROVE / WARN / REJECT)
    → [existing RiskManager]    (final authority)
    → [existing Execution]      (only if LiveModeGuard allows)
```

## Research / improvement loop

```
Experience → Reflection → Hypothesis → Experiment
  → Backtest → Walk-forward → Stress → Shadow → Promotion (human by default)
```

## Optional observation loop (Phase 21)

```bash
export ARBICORE_AGENT_LOOP=1   # default is off
```

When enabled, `AgentObservationLoop` runs the orchestrator on price updates,
records regimes / selections / critiques, and **never places orders**.
Existing scan + execution behaviour is unchanged.

## Modules

| Module | Phase | Role |
|--------|-------|------|
| `domain` / `guards` | 1 | Models + LiveModeGuard |
| `memory` | 2 | Experience + audit persistence |
| `features` | 3 | FeatureEngine |
| `regime` | 4 | RegimeDetector |
| `strategy_registry` | 5 | Versioned strategies |
| `selection` | 6 | Strategy selection |
| `critic` | 7 | CriticAgent |
| `reflection` | 8 | ReflectionAgent |
| `research` | 9–10 | Hypothesis + Experiment |
| `backtest` | 11 | Backtest harness |
| `validation` | 12–13 | Walk-forward + stress |
| `shadow` | 14–15 | Shadow portfolio |
| `promotion` | 16–17 | Champion / Challenger |
| `pipeline` | 18 | Research evaluation pipeline |
| `orchestrator` | 20 | Decision cycle wiring |
| `agent_loop` | 21 | Feature-flagged observation (no execution) |

## Safety (unchanged)

* RiskManager is the final pre-trade gate.
* Real orders need live mode + real execution + exact ack + credentials.
* Agents never call the exchange.
* Agents never raise risk limits.
* Observation loop defaults **off** and only emits NO_TRADE proposals.

## Not done yet

* Dashboard Strategy Lab / Research Lab UI
* Wiring a real entry-signal engine into TradeProposal (still NO_TRADE)
* Live canary capital allocation
* ML / RL

## Run the original bot

```bash
pip install -r requirements.txt
python server.py
```
