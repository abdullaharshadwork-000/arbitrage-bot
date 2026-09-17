# ArbiCore Agentic Foundation

**Risk Kernel, LiveModeGuard, and real-trading gates remain the highest authority.**

## Decision → paper path

```
Prices → Features → Regime → Selection → SignalEngine → Critic
                                              │
                         APPROVE only ────────┤
                                              ▼
                                    PaperExecutor (simulated fill)
                                              │
                                    [Live exchange: NOT connected here]
```

`PaperExecutor` refuses LIVE mode construction, REJECT/WARN, and NO_TRADE.

## Research loop

```
Experience → Reflection → Hypothesis → Experiment
  → Backtest → Walk-forward → Stress → Shadow → Promotion (human default)
```

## Optional observation

```bash
export ARBICORE_AGENT_LOOP=1
python scripts/enable_agent_api.py
python server.py
# GET /api/agent  |  GET /api/agent/strategies
```

## Modules (Phases 1–24)

| Module | Phase | Role |
|--------|-------|------|
| domain / guards | 1 | Models + LiveModeGuard |
| memory | 2 | Experience + audit |
| features / regime | 3–4 | Features + regime |
| strategy_registry / selection / critic | 5–7 | Strategies + critique |
| reflection / research | 8–10 | Learning |
| backtest / validation | 11–13 | Evaluation |
| shadow / promotion / pipeline | 14–18 | Challenger + research pipeline |
| orchestrator / agent_loop / agent_api | 20–22 | Cycle + observation + API |
| signals_agent | 23 | TradeProposal signals |
| paper_exec | 24 | Paper fills after Critic APPROVE |

## Safety

* No agent path places live orders.
* PaperExecutor cannot be constructed in LIVE mode.
* Real trading still requires existing Settings.real gates.

```bash
pip install -r requirements.txt
python server.py
```
