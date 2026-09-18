# ArbiCore Agentic Platform

**Risk Kernel + LiveModeGuard remain highest authority. Agents never call Binance.**

## Operator UI

After enabling the lab blueprint:

* **http://127.0.0.1:5050/lab** — Strategy Lab dashboard
* **http://127.0.0.1:5050/api/lab** — JSON aggregate

## Enable (Windows PowerShell)

```powershell
git pull origin main
python scripts\enable_agent_api.py
$env:ARBICORE_AGENT_LOOP = "1"
$env:ARBICORE_AGENT_PAPER_EXEC = "1"
$env:ARBICORE_AGENT_HANDOFF = "1"
python server.py
```

Open `http://127.0.0.1:5050/lab` (use your port if different).

## Architecture

```
Observe → Features → Regime → Select → Signal → Critic
  → OrderIntent → RiskAdapter → Handoff queue
  → PaperExecutor (optional)
  → Experience → Reflection → Patterns → Drift → Scorecard
  → Canary (operator stages)
  → ML registry / HeuristicScorer / RL research (offline only)
```

## Safety

* No agent path places exchange orders
* LIVE only via existing Settings + LiveModeGuard + real ack
* Handoff only queues ApprovedOrderRequest
* Canary only tracks allocation fractions
* ML/RL never wired to live execution
