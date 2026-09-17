# ArbiCore Agentic Foundation

**Risk Kernel, LiveModeGuard, and real-trading gates remain the highest authority.**

## Decision path

```
Prices → Features → Regime → Selection → SignalEngine → Critic
                                              │
                                    APPROVE only (optional)
                                              ▼
                                      PaperExecutor
                                              ▼
                                      ExperienceMemory
                                              ▼
                                      Reflection (later)

Live exchange: not connected on this path.
```

## Feature flags (all default OFF)

```bash
export ARBICORE_AGENT_LOOP=1          # run observation cycles
export ARBICORE_AGENT_PAPER_EXEC=1    # paper-fill after Critic APPROVE
python scripts/enable_agent_api.py    # optional GET /api/agent
python server.py
```

## Modules (Phases 1–25)

| Module | Phase | Role |
|--------|-------|------|
| domain / guards | 1 | Models + LiveModeGuard |
| memory | 2 | Experience + audit |
| features / regime | 3–4 | Features + regime |
| strategy_registry / selection / critic | 5–7 | Strategies + critique |
| reflection / research | 8–10 | Learning |
| backtest / validation | 11–13 | Evaluation |
| shadow / promotion / pipeline | 14–18 | Challenger + research |
| orchestrator / agent_loop / agent_api | 20–22 | Cycle + observation + API |
| signals_agent | 23 | TradeProposal signals |
| paper_exec | 24 | Paper fills after APPROVE |
| agent_loop (extended) | 25 | Paper fill + Experience record |

## Safety

* Observation loop and paper exec refuse LIVE mode.
* No agent path places live orders.
* Existing bot + Risk Kernel unchanged.

```bash
pip install -r requirements.txt
python server.py
```
