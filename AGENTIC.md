# ArbiCore Agentic Platform — Agent Live Orders

## End-to-end path

```
Scan mids → Agent loop → Critic APPROVE
  → RiskManager.check → Handoff queue
  → maybe_process_handoff → LiveExecutor → RealExecutionEngine place_market_*
```

## Enable (Windows)

```powershell
git pull origin main
python scripts\fix_project.py
# patches server.py for lab + live hooks

$env:ARBICORE_AGENT_LOOP = "1"
$env:ARBICORE_AGENT_HANDOFF = "1"
$env:ARBICORE_AGENT_LIVE_EXEC = "1"
$env:ARBICORE_AGENT_CANARY = "0.05"   # 5% of approved notional
python server.py
```

Or: `.\scripts\start_agent_live.ps1`

## Dashboard (required)

1. Mode **live**
2. Execution **real**
3. Type the real-trading acknowledgement
4. Connect exchange API keys (trade only)
5. Start the bot so `RealExecutionEngine` builds

When the real engine starts, the server hook **auto-registers** the place_fn.
Each scan processes up to 1 queued agent order (canary-sized).

## Status

* `GET /api/lab/live` — flags, wire_registered, risk_context, recent fills
* `GET /api/lab` — full lab including live section

## Gates (cannot be skipped by the agent)

| Gate | Role |
|------|------|
| Critic APPROVE | Proposal quality |
| RiskManager | Notional / limits |
| LiveModeGuard | live+real+ack |
| ARBICORE_AGENT_LIVE_EXEC | Explicit opt-in |
| Canary fraction | Size reduction |
| RealExecutionEngine | Same path as main bot |

Without any one of these, no agent live order is sent.
