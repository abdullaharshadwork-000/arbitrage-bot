# ArbiCore Agentic Foundation (Phases 1–27)

**Risk Kernel + LiveModeGuard remain highest authority. No agent path places live orders.**

## Pipeline

```
Scan mid prices → notify_agent_mid (optional)
    → Features → Regime → Select → Signal → Critic
                              APPROVE + paper flag → PaperExecutor
                                                   → Experience → Reflection
```

## Enable (one script, flags still required)

```bash
python scripts/enable_agent_api.py   # patches server.py (API + scan hook)
export ARBICORE_AGENT_LOOP=1
export ARBICORE_AGENT_PAPER_EXEC=1   # optional paper fills
python server.py
```

Then:

* Each scan pushes mid prices into the agent loop (no-op if flags off)
* `GET /api/agent` – status, cycles, paper fills, reflections
* `GET /api/agent/strategies` – registry (demo strategy seeded)

## Safety

* `notify_agent_mid` never raises into the scanner
* Loop / PaperExecutor refuse LIVE mode
* Existing arbitrage execution path unchanged until you change Risk / Live gates yourself

```bash
pip install -r requirements.txt && python server.py
```
