# ArbiCore Agentic Foundation

**Risk Kernel + LiveModeGuard remain highest authority. Agents never call Binance.**

## Decision path (now fully gated)

```
TradeProposal
    → CriticAgent          (APPROVE / WARN / REJECT)
    → OrderIntentBridge    (intent only if APPROVE + BUY/SELL)
    → RiskAdapter          (existing RiskManager.check)
    → ApprovedOrderRequest (still NOT an exchange order)

Only the existing server/execution path may send real orders,
and only when Settings.real + LiveModeGuard already allow it.
```

## Observation path

```
Scan mid → Features → Regime → Select → Signal → Critic
  → PaperExecutor (optional flag)
  → ExperienceMemory → Reflection → Patterns → Drift → Scorecard
```

## Enable (Windows PowerShell)

```powershell
git pull origin main
python scripts\enable_agent_api.py
$env:ARBICORE_AGENT_LOOP = "1"
$env:ARBICORE_AGENT_PAPER_EXEC = "1"
python server.py
```

## API

`/api/agent`, `/strategies`, `/research`, `/drift`, `/patterns`, `/similarity`, `/scorecard`

## Remaining (by design)

* Consume `ApprovedOrderRequest` inside existing scan/execution (optional flag)
* Live canary allocation
* Dashboard Strategy Lab UI
* ML / RL research-only

Never weaken RiskManager limits or LiveModeGuard.
