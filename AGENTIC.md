# ArbiCore Agentic Foundation (Phases 1–27)

**Risk Kernel + LiveModeGuard remain highest authority. No agent path places live orders.**

## Pipeline

```
Prices → Features → Regime → Select → Signal → Critic
                                              │
                                    APPROVE + paper flag
                                              ▼
                                      PaperExecutor → Experience → Reflection
```

## Enable observation (default OFF)

```bash
export ARBICORE_AGENT_LOOP=1
export ARBICORE_AGENT_PAPER_EXEC=1   # optional paper fills
python scripts/enable_agent_api.py  # optional GET /api/agent
python server.py
```

## Hook from the existing scanner (optional)

After you have a per-symbol price history list:

```python
from arbicore.scan_hook import notify_agent_prices

notify_agent_prices("BTC/USDT", price_history)  # never raises; no-op if flags off
```

## API (after enable script)

* `GET /api/agent` – cycles, paper fills, reflections, recent history
* `GET /api/agent/strategies` – registry (includes seeded demo strategy)

## Safety

* Loop / PaperExecutor refuse LIVE mode
* `notify_agent_prices` swallows all errors
* Existing arbitrage execution path unchanged

```bash
pip install -r requirements.txt && python server.py
```
