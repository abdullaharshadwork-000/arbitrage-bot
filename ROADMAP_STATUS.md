# ArbiCore Agentic Roadmap Status

Generated as of the remaining-tasks completion pass.

## Module inventory (code present on `main`)

| Area | Modules | Status |
|------|---------|--------|
| Domain / modes / guards | `domain`, `guards`, `config` | Done |
| Features / regime | `features`, `regime` | Done |
| Strategy registry / selection | `strategy_registry`, `selection` | Done |
| Critic / reflection / research | `critic`, `reflection`, `research`, `patterns`, `knowledge` | Done |
| Memory / similarity / drift | `memory`, `similarity`, `drift`, `scorecard` | Done |
| Paper / shadow / pipeline | `paper_exec`, `shadow`, `pipeline`, `validation` | Done |
| Promotion / canary / gate | `promotion`, `promotion_gate`, `canary` | Done |
| Live path | `live_bridge`, `risk_adapter`, `live_exec`, `live_place`, `agent_live_wire`, `server_live_hook` | Done |
| Positions / trail / learn | `agent_positions`, `agent_store`, `trailing`, `agent_learn` | Done |
| Kill / reconcile / notify | `kill_switch`, `agent_reconcile`, `notify` | Done |
| Portfolio / MC / auto-improve | `portfolio_alloc`, `monte_carlo`, `auto_improve` | Done |
| ML / RL research | `ml_registry`, `ml_scorer`, `rl_research` | Scaffold Done |
| Lab UI | `/lab`, `/api/lab`, `static/agent_lab.html` | Done |
| Glass theme | `static/theme-glass.css`, `scripts/enable_glass_theme.py` | Done |

## What “done” means here

**Code modules** for the Observe → Risk → Execute → Learn → Promote path are in the repository.

**Not the same as** unsupervised production trading for months with proven economic edge.

## Still operator / ops work (cannot be finished by code alone)

1. **Soak test** – paper days, then testnet, then 5% canary with real capital.
2. **Glass theme** – run `python scripts/enable_glass_theme.py` and hard-refresh.
3. **One server only** – clear worker leases; never two `server.py`.
4. **Human approval** on promotion gate before LIVE canary.
5. **Scenario drills** – WS disconnect, restart with open position, kill switch trip.
6. **Full Command Center polish** – Lab exists; main dashboard agent widgets optional.
7. **ML/RL** – research-only; not connected to unrestricted LIVE.

## Safety boundaries (unchanged)

* Risk Kernel is final.
* No LLM → Binance direct.
* LIVE requires flags + LiveModeGuard + kill switch clear + canary.
* PromotionGate requires human_approval by default.

## Enable LIVE agent (controlled)

```powershell
git pull origin main
python scripts\fix_project.py
python scripts\enable_glass_theme.py

$env:ARBICORE_AGENT_LOOP = "1"
$env:ARBICORE_AGENT_HANDOFF = "1"
$env:ARBICORE_AGENT_LIVE_EXEC = "1"
$env:ARBICORE_AGENT_CANARY = "0.05"
python server.py
```

Trip kill switch anytime: `$env:ARBICORE_KILL = "1"` then restart, or call `get_kill_switch().trip(...)`.

## Approximate completion

| Layer | ~% |
|-------|----|
| Architecture / modules | ~85–90% |
| Integration with existing engine | ~70% |
| Production validation / soak | ~20% |
| Full self-improving economic proof | ongoing |

The platform is **code-complete for the designed agentic stack**. Treat real-money scaling as an operations program, not a missing source file.
