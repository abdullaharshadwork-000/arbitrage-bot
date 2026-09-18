# Safety Boundaries (Permanent)

## Unrestricted autonomous real trading “always on”

**Status: Will not be implemented. This is intentional.**

Your specification requires:

> REAL-MONEY TRADING = YES  
> UNCONTROLLED REAL-MONEY EXPERIMENTATION = NO

Completing “always on unrestricted” would violate that principle.

### What LIVE is allowed to do (when *you* enable it)

With all of these true at once:

1. Operator sets trading mode live + real execution + acknowledgement in UI  
2. `ARBICORE_AGENT_LIVE_EXEC=1`  
3. LiveModeGuard allows real orders  
4. Risk Kernel approves size  
5. Kill switch is clear  
6. Canary fraction < 1 (default 0.05)  
7. Wire registered to existing exchange client  

…then **approved** strategies may open/close/trail **without per-trade human clicks**.

That is autonomous execution of **approved** flow — not unrestricted experimentation.

### What will never be autonomous

| Action | Autonomous? |
|--------|-------------|
| Enable LIVE mode | No |
| Raise hard risk limits | No |
| Disable kill switch permanently | No |
| Bypass promotion gate | No |
| LLM call Binance API | No |
| Deploy unvalidated strategy at full size | No |
| Withdrawals | Never |

### Self-improvement with evidence

Code records evidence (`evidence_loop`, paper soak, Lab scorecard).  
**Proof** only appears after you run the operator phases over calendar time.

See `OPERATOR_RUNBOOK.md`.
