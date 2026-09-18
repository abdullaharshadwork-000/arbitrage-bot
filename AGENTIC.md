# ArbiCore Agentic Platform

## Live agent path

```
Scan → Agent → Critic APPROVE → RiskManager → Handoff
  → LiveExecutor (canary) → open position (SQLite)
  → mid hits SL/TP → full exit → close position
  → ExperienceMemory (learn) → scorecard / reflection
```

## Enable (one server only)

```powershell
git pull origin main
python scripts\fix_project.py

$env:ARBICORE_AGENT_LOOP = "1"
$env:ARBICORE_AGENT_HANDOFF = "1"
$env:ARBICORE_AGENT_LIVE_EXEC = "1"
$env:ARBICORE_AGENT_CANARY = "0.05"
python server.py
```

Dashboard: live + real + ack → **Start Engine**.

## Lab

* `http://127.0.0.1:5050/lab` — Strategy Lab UI
* `http://127.0.0.1:5050/api/lab` — JSON (agent, live wire, positions)

Open positions are stored in `arbicore_agent.db` and restored after restart.
