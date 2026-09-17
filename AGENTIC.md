# ArbiCore Agentic Foundation (Phases 1–29)

**Risk Kernel + LiveModeGuard remain highest authority. No agent path places live orders.**

## Pipeline

```
Scan mid prices → notify_agent_mid (optional)
  → Features → Regime → Select → Signal → Critic
        APPROVE + paper flag → PaperExecutor → Experience → Reflection
                                              → DriftMonitor (API)
```

## Enable (Windows PowerShell)

```powershell
git pull origin main
python scripts\enable_agent_api.py
$env:ARBICORE_AGENT_LOOP = "1"
$env:ARBICORE_AGENT_PAPER_EXEC = "1"   # optional
python server.py
```

## API routes (after enable script)

| Route | Purpose |
|-------|---------|
| `GET /api/agent` | Observation cycles, paper fills, reflections |
| `GET /api/agent/strategies` | Strategy registry |
| `GET /api/agent/research` | Hypotheses / experiments (when lab attached) |
| `GET /api/agent/drift` | Drift reports per strategy |

## Safety

* Hooks never raise into the scanner
* Loop / PaperExecutor refuse LIVE mode
* Drift is read-only analytics

```powershell
pip install -r requirements.txt
python server.py
```
