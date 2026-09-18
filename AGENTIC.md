# ArbiCore Agentic Platform — Agent Live Orders

## End-to-end path

```
Scan mids → Agent loop → Critic APPROVE
  → RiskManager.check → Handoff queue
  → maybe_process_handoff → LiveExecutor → RealExecutionEngine place_market_*
  → AgentPositionBook (open with SL/TP)
  → on later scans: mid hits SL/TP → exit order (full size, no canary)
```

## Enable (Windows)

1. **One** `python server.py` only (avoid "Another supervised worker owns this account").
2. Clear stale lease if needed:
   `python -c "import sqlite3; c=sqlite3.connect('arbicore.db'); c.execute('DELETE FROM worker_leases'); c.commit()"`

```powershell
git pull origin main
python scripts\fix_project.py

$env:ARBICORE_AGENT_LOOP = "1"
$env:ARBICORE_AGENT_HANDOFF = "1"
$env:ARBICORE_AGENT_LIVE_EXEC = "1"
$env:ARBICORE_AGENT_CANARY = "0.05"
python server.py
```

Dashboard: **live** + **real** + ack → **Start Engine** once.

## Status

* `GET /api/lab/live` — wire, risk, canary, **positions**
* `GET /lab` — Strategy Lab UI

## Gates

Critic · RiskManager · LiveModeGuard · LIVE_EXEC flag · Canary (entries) · RealExecutionEngine
