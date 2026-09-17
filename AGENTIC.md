# ArbiCore Agentic Foundation

Agentic / self-improving layers on top of the original arbitrage engine.
**Nothing here replaces or weakens the existing Risk Kernel, LiveModeGuard, or real-trading gates.**

## Live decision pipeline

```
Market prices
    → FeatureEngine → RegimeDetector → StrategySelector
    → TradeProposal → CriticAgent
    → [RiskManager] → [Execution only if LiveModeGuard allows]
```

## Research loop

```
Experience → Reflection → Hypothesis → Experiment
  → Backtest → Walk-forward → Stress → Shadow → Promotion (human default)
```

## Optional observation loop

```bash
export ARBICORE_AGENT_LOOP=1   # default off
```

Runs the orchestrator on price updates. **Never places orders.**

## Read-only agent API (Phase 22)

Helpers: `arbicore/agent_api.py`

Routes (after registration):

* `GET /api/agent` – observation loop snapshot
* `GET /api/agent/strategies` – strategy registry

### Enable routes in server.py (one-time, safe)

```bash
python scripts/enable_agent_api.py
# restart server
python server.py
```

The script is idempotent. It wraps registration in try/except so a failure
never prevents the main bot from starting. No trading behaviour changes.

## Module map

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

## Safety

* RiskManager remains final pre-trade gate.
* Real orders need live + real + exact ack + credentials.
* Agents never call the exchange or raise risk limits.
* Agent loop and API are observation-only by default.

## Run the original bot

```bash
pip install -r requirements.txt
python server.py
```
