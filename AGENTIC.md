# ArbiCore Agentic Platform

**Risk Kernel + LiveModeGuard remain highest authority. Agents never call Binance.**

## Quick fix / start (Windows)

```powershell
git pull origin main
python scripts\fix_project.py
# or one-shot:
.\scripts\start_agent_paper.ps1
```

`fix_project.py` patches `server.py` for agent + Strategy Lab routes and verifies imports.

## Flags

```powershell
$env:ARBICORE_AGENT_LOOP = "1"
$env:ARBICORE_AGENT_PAPER_EXEC = "1"
# $env:ARBICORE_AGENT_HANDOFF = "1"   # queue only; still no auto live send
python server.py
```

## Operator UI

* Main dashboard: **Strategy Lab** in sidebar + agent status on Trading terminal
* **http://127.0.0.1:5050/lab** — Strategy Lab page
* **http://127.0.0.1:5050/api/lab** — JSON aggregate

## Safety

* No agent path places exchange orders
* LIVE only via existing Settings + LiveModeGuard + real ack
* Handoff only queues ApprovedOrderRequest
* Critic may use HeuristicScorer as a research signal only
