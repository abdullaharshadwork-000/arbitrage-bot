# ArbiCore Agentic Platform — Roadmap Foundations Complete

**Risk Kernel + LiveModeGuard remain highest authority. Agents never call Binance.**

## Full loop implemented (foundations)

```
Observe → Features → Regime → Select → Signal → Critic
  → OrderIntent → RiskAdapter → Handoff queue (optional)
  → PaperExecutor (optional)
  → Experience → Reflection → Patterns → Similarity → Drift → Scorecard
  → Research / Backtest / Walk-forward / Stress / Shadow / Promotion
  → Canary stages (operator-driven)
  → ML registry + RL research env (research-only)
```

## Enable (Windows PowerShell)

```powershell
git pull origin main
python scripts\enable_agent_api.py
$env:ARBICORE_AGENT_LOOP = "1"
$env:ARBICORE_AGENT_PAPER_EXEC = "1"
$env:ARBICORE_AGENT_HANDOFF = "1"   # queue risk-cleared intents only
python server.py
```

## APIs

| Route | Purpose |
|-------|---------|
| `/api/agent` | Observation state |
| `/api/agent/strategies` | Strategy registry |
| `/api/agent/research` | Hypotheses / experiments |
| `/api/agent/drift` | Drift reports |
| `/api/agent/patterns` | Pattern discovery |
| `/api/agent/similarity` | Historical similarity |
| `/api/agent/scorecard` | Self-improvement metrics |
| `/api/lab` | Full lab aggregate |
| `/api/lab/canary` | Canary deployments |
| `/api/lab/models` | ML model registry |
| `/api/lab/handoff` | Queued approved intents |

## Safety boundaries (unchanged)

* No agent path places exchange orders
* LIVE requires existing Settings + LiveModeGuard + real ack
* RiskManager.check is mandatory for ApprovedOrderRequest
* Canary only computes allocation fractions
* ML / RL are research-only and not wired to execution

## What “complete” means here

All planned **foundation modules** for Phases 0–26 exist in-repo.
Operator must still:

1. Pull + run enable script + set flags
2. Run paper/shadow long enough to collect experiences
3. Explicitly enable LIVE via existing bot settings when ready
4. Advance canary stages manually via `/api/lab/canary`

Further production hardening (richer UI polish, trained ML weights, automated canary promotion) is incremental product work on top of this architecture.
