# Development complete (code program)

This document closes the **implementation** phase of the agentic ArbiCore transformation.

## Delivered

* Modular `arbicore/` agent stack (observe → critique → risk → paper/live → learn)
* Strategy Lab (`/lab`, `/api/lab`, external JS for CSP)
* Live agent path (gated): handoff → canary → place → positions → trail → experience
* Kill switch, reconcile hooks, promotion gate, champion comparison, evidence/soak tools
* Session wire so scan loop registers agent + live context
* Safety boundaries documented (`SAFETY_BOUNDARIES.md`)
* Operator evidence process (`OPERATOR_RUNBOOK.md`)

## Not delivered (by design or by nature)

| Item | Why |
|------|-----|
| Unrestricted always-on LIVE | Violates core safety principle |
| Multi-month production proof | Requires calendar time running the bot |
| Guaranteed profitable strategies | Markets; not a coding deliverable |

## Operator activate checklist

```powershell
git pull origin main
Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force
python scripts\fix_project.py

$env:ARBICORE_AGENT_LOOP = "1"
$env:ARBICORE_AGENT_PAPER_EXEC = "1"
python server.py
```

Verify: `/agent_lab.js`, `/api/lab`, `/lab` (hard refresh). Start engine → cycles increase.

Controlled LIVE (optional):

```powershell
$env:ARBICORE_AGENT_HANDOFF = "1"
$env:ARBICORE_AGENT_LIVE_EXEC = "1"
$env:ARBICORE_AGENT_CANARY = "0.05"
```

Dashboard: live + real + ack → Start Engine. One worker only.

## Further work is operations

Paper soak → Lab metrics → testnet → 5% canary → promotion gate → evidence history.

No further architecture phases are required to begin that process.
