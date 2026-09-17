# ArbiCore Agentic Foundation (Phases 1–26)

**Risk Kernel, LiveModeGuard, and real-trading gates remain highest authority.**

## Decision path

```
Prices → Features → Regime → Selection → Signal → Critic
                                              │
                                    APPROVE + paper flag
                                              ▼
                                      PaperExecutor
                                              ▼
                                      Experience + Reflection

Live exchange: not on this path.
```

## Flags (default OFF)

```bash
export ARBICORE_AGENT_LOOP=1
export ARBICORE_AGENT_PAPER_EXEC=1
python scripts/enable_agent_api.py   # optional
python server.py
```

On first loop init, `seed_demo_strategy()` registers an APPROVED
`momentum_regime_v1` strategy for paper/observation demos only.

## Module map

| Module | Phase | Role |
|--------|-------|------|
| domain / guards | 1 | Models + LiveModeGuard |
| memory | 2 | Experience + audit |
| features / regime | 3–4 | Features + regime |
| strategy_registry / selection / critic | 5–7 | Strategies + critique |
| reflection / research | 8–10 | Learning |
| backtest / validation | 11–13 | Evaluation |
| shadow / promotion / pipeline | 14–18 | Challenger + research |
| orchestrator / agent_loop / agent_api | 20–22 | Cycle + API |
| signals_agent / paper_exec | 23–24 | Signals + paper fills |
| agent_loop + bootstrap | 25–26 | Experience, reflection, demo seed |

## Safety

* Loop and PaperExecutor refuse LIVE mode.
* No agent path places live orders.
* Demo strategy is not live authorization.

```bash
pip install -r requirements.txt && python server.py
```
