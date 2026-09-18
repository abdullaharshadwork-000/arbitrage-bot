# ArbiCore Agentic Foundation

**Risk Kernel + LiveModeGuard remain highest authority. Agents never call Binance.**

## Built pipeline

```
Scan → Features → Regime → Select → Signal → Critic
         │                                    │
         │                         APPROVE only
         │                                    ▼
         │                         PaperExecutor / OrderIntentBridge
         │                                    │
         └────────── ExperienceMemory ────────┤
                                              ▼
                    Reflection → Patterns → Similarity → Drift → Scorecard
```

## Enable (Windows PowerShell)

```powershell
git pull origin main
python scripts\enable_agent_api.py
$env:ARBICORE_AGENT_LOOP = "1"
$env:ARBICORE_AGENT_PAPER_EXEC = "1"
python server.py
```

## API routes

| Route | Purpose |
|-------|---------|
| `/api/agent` | Cycles, paper fills, memory |
| `/api/agent/strategies` | Registry |
| `/api/agent/research` | Hypotheses / experiments |
| `/api/agent/drift` | Performance drift |
| `/api/agent/patterns` | Pattern discovery |
| `/api/agent/similarity` | Historical similar states |
| `/api/agent/scorecard` | Self-improvement scorecard |

## Modules (spec phases)

| Area | Modules |
|------|---------|
| Domain / guards | domain, guards |
| Memory | memory, knowledge |
| Market intelligence | features, regime, similarity |
| Strategy | strategy_registry, selection, bootstrap |
| Decision | signals_agent, critic, orchestrator |
| Execution (safe) | paper_exec, live_bridge (intent only) |
| Learning | reflection, research, patterns, drift, scorecard |
| Validation | backtest, validation, shadow, pipeline, promotion |
| Integration | agent_loop, scan_hook, agent_api |

## Still deferred (by design)

* Wiring OrderIntent into existing Risk Kernel → real Binance orders
* Live canary capital stages
* Dashboard Strategy Lab UI
* Supervised ML / RL (research-only when added)

These must not weaken Risk Kernel or LiveModeGuard.
