# ArbiCore Agentic Foundation

Agentic layers on top of the original arbitrage engine.
**Risk Kernel, LiveModeGuard, and real-trading gates are unchanged and remain highest authority.**

## Live decision pipeline

```
Market prices
    → FeatureEngine
    → RegimeDetector
    → StrategySelector
    → SignalEngine              (TradeProposal; may be NO_TRADE)
    → CriticAgent               (APPROVE / WARN / REJECT)
    → [RiskManager]             (final authority – existing code)
    → [Execution]               (only if LiveModeGuard allows)
```

**Important:** The orchestrator and signal engine never call the exchange.
Execution still only happens through the existing bot path after Risk + Live gates.

## Research loop

```
Experience → Reflection → Hypothesis → Experiment
  → Backtest → Walk-forward → Stress → Shadow → Promotion (human default)
```

## Optional observation loop

```bash
export ARBICORE_AGENT_LOOP=1   # default off
python scripts/enable_agent_api.py   # one-time, optional API routes
python server.py
```

* `GET /api/agent` – observation snapshot (after enable script)
* `GET /api/agent/strategies` – registry listing

## Module map (Phases 1–23)

| Module | Phase | Role |
|--------|-------|------|
| domain / guards | 1 | Models + LiveModeGuard |
| memory | 2 | Experience + audit |
| features / regime | 3–4 | Features + regime |
| strategy_registry / selection / critic | 5–7 | Strategies + critique |
| reflection / research | 8–10 | Learning loop |
| backtest / validation | 11–13 | Evaluation + stress |
| shadow / promotion / pipeline | 14–18 | Challenger + research pipeline |
| orchestrator / agent_loop / agent_api | 20–22 | Decision cycle + observation + API |
| signals_agent | 23 | TradeProposal from features/regime |

## Safety

* Agents never place orders.
* Real trading still needs live mode + real execution + exact ack + credentials.
* Critic and Risk Kernel remain in front of any future execution wiring.

## Run the original bot

```bash
pip install -r requirements.txt
python server.py
```
