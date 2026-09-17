# ArbiCore Agentic Foundation (Phases 1–30)

**Risk Kernel + LiveModeGuard remain highest authority. No agent path places live orders.**

## Pipeline

```
Scan mid prices → notify_agent_mid
  → Features → Regime → Select → Signal → Critic
        APPROVE + paper flag → PaperExecutor
                             → ExperienceMemory (arbicore_agent.db)
                             → Reflection → Drift API
```

## Enable (Windows PowerShell)

```powershell
git pull origin main
python scripts\enable_agent_api.py
$env:ARBICORE_AGENT_LOOP = "1"
$env:ARBICORE_AGENT_PAPER_EXEC = "1"
python server.py
```

Optional memory path:

```powershell
$env:ARBICORE_AGENT_DB = "C:\path\to\arbicore_agent.db"
```

## API

| Route | Purpose |
|-------|---------|
| `GET /api/agent` | Cycles, paper fills, reflections, memory stats |
| `GET /api/agent/strategies` | Registry |
| `GET /api/agent/research` | Research lab |
| `GET /api/agent/drift` | Drift reports |

## Safety

* Hooks never raise into the scanner
* Loop / PaperExecutor refuse LIVE mode
* Memory is additive (`arbicore_agent.db`), separate from main trade DB
