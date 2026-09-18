# ArbiCore Agentic Platform

## Agent live orders (REAL money)

Agents **can** place live orders, but only through this gated path:

```
TradeProposal → Critic APPROVE → OrderIntent → RiskManager.check
  → ApprovedOrderRequest → Handoff → LiveExecutor.place_fn
  → existing submit_market_order (same as main bot)
```

### Required gates (all must pass)

1. Dashboard: **mode = live**, **execution = real**, exact **real trading ack**
2. Exchange API keys connected (trade only, no withdraw)
3. Env: `ARBICORE_AGENT_LIVE_EXEC=1` and `ARBICORE_AGENT_HANDOFF=1`
4. `register_live_wire(place_fn, live_guard=..., canary_fraction=0.05)` called once at startup
5. Risk Kernel allows the notional

Default canary = **5%** of approved notional.

### Example wire (call once after you have a live ccxt client)

```python
from arbicore.live_place import make_place_fn
from arbicore.agent_live_wire import register_live_wire
from arbicore.guards import LiveModeGuard
from arbicore.config import Settings

place_fn = make_place_fn(client, "binance")
register_live_wire(
    place_fn,
    live_guard=LiveModeGuard(Settings.from_environ()),  # or your settings object
    canary_fraction=0.05,
)
```

Then process queued intents (e.g. from a timer or after each scan):

```python
from arbicore.agent_live_wire import process_handoff_queue
process_handoff_queue(max_items=1)
```

### Windows start (flags only – still need dashboard live+ack + register_live_wire)

```powershell
.\scripts\start_agent_live.ps1
```

### Status

* `GET /api/lab/live` – wire registered? flag on? recent results
* `GET /api/lab` – full lab aggregate including `live`

### Safety

* Agents never hold API keys or construct ccxt clients
* LiveModeGuard + Risk Kernel cannot be skipped by the agent
* Without `ARBICORE_AGENT_LIVE_EXEC=1`, live path is off
