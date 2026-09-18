# ArbiCore Roadmap Status

## Layer scorecard

| Layer | Complete? | How |
|-------|-----------|-----|
| Modular agentic architecture | **Yes** | `arbicore/*` modules on main |
| Controlled LIVE agent path (gated) | **Yes** | flags + Risk + canary + kill + wire |
| Unrestricted always-on LIVE | **No — by design** | See `SAFETY_BOUNDARIES.md` |
| Self-improving with evidence | **Tooling yes; proof needs runtime** | `evidence_loop` + `run_paper_soak.py` + Lab |
| Multi-month research org | **Process defined** | `OPERATOR_RUNBOOK.md` Phases A–D |

## Why “always-on unrestricted” will stay incomplete

Implementing it would break the core safety principle in the product spec.  
Controlled autonomy (approved trades after gates) **is** implemented.

## How to complete evidence / research-org behavior

1. `python scripts/run_paper_soak.py --cycles 50`  
2. Run server paper agent days; watch `/lab`  
3. Follow `OPERATOR_RUNBOOK.md` → canary only when ready  
4. Compare evidence snapshots over weeks  

## Enable controlled LIVE (not unrestricted)

```powershell
git pull origin main
python scripts\fix_project.py
$env:ARBICORE_AGENT_LOOP="1"
$env:ARBICORE_AGENT_HANDOFF="1"
$env:ARBICORE_AGENT_LIVE_EXEC="1"
$env:ARBICORE_AGENT_CANARY="0.05"
python server.py
```

Kill: `$env:ARBICORE_KILL="1"`
