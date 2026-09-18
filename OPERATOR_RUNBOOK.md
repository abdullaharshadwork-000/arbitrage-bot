# Operator Runbook – Evidence & Controlled LIVE

This closes the gap that **code alone cannot**: production evidence and multi-month research behavior.

## Layer status (honest)

| Layer | How it gets “complete” |
|-------|------------------------|
| Modular architecture | Already in repo |
| Controlled LIVE (gated) | Flags + Risk + canary + kill switch |
| **Unrestricted always-on LIVE** | **Never – by design (see SAFETY)** |
| Self-improving with evidence | Paper soak → testnet → canary over **time** |
| Multi-month research org | Same + promotion gate + scorecard history |

---

## Phase A – Paper evidence (no real money)

```powershell
git pull origin main
python scripts\fix_project.py

$env:ARBICORE_AGENT_LOOP = "1"
$env:ARBICORE_AGENT_PAPER_EXEC = "1"
# do NOT set LIVE_EXEC

python scripts\run_paper_soak.py --cycles 50
python server.py
```

Open `/lab` – Agent / Scorecard should move.  
Repeat soaks daily for a week. Evidence DB: `arbicore_evidence.db`.

**Done when:** scorecard history has many snapshots; trend_hint is readable.

---

## Phase B – Testnet / demo (if your exchanges support it)

Use existing testnet/demo adapters only. Keep `ARBICORE_AGENT_CANARY=0.05`.  
Still no full-size LIVE.

---

## Phase C – Controlled LIVE canary (real capital, small)

**Only after Phase A is healthy.**

```powershell
$env:ARBICORE_AGENT_LOOP = "1"
$env:ARBICORE_AGENT_HANDOFF = "1"
$env:ARBICORE_AGENT_LIVE_EXEC = "1"
$env:ARBICORE_AGENT_CANARY = "0.05"
# one server only
python server.py
```

Dashboard: **live + real + acknowledgement** → Start Engine.  
PromotionGate must have **human_approval** before increasing canary.

Trip kill anytime: `$env:ARBICORE_KILL="1"` and restart, or Lab/API kill.

**Done when:** days of fills, no unexpected positions, reconcile OK, kill switch tested.

---

## Phase D – Multi-month research behavior

1. Weekly: run soak / review Lab scorecard + drift  
2. On drift: auto_improve proposes hypothesis (no auto LIVE)  
3. Candidate → backtest → WF → stress → shadow → paper → gate → canary  
4. Rollback if live expectancy diverges  

**Done when:** you can point to later versions with better risk-adjusted metrics than earlier ones **with stored evidence**.

---

## Never complete on purpose

- Unrestricted always-on without flags  
- LLM direct to Binance  
- AI raising hard risk limits  
- Silent LIVE enable  
- Auto-promote without human_approval (unless you deliberately change policy later)  
