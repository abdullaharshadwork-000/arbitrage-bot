# Development complete

**Status: implementation program closed.**

Further progress is **operations** (run paper → Lab metrics → optional canary), not new architecture phases.

## Last code additions

* `scripts/apply_server_patches.py` — Lab routes + session wire on `server.py`
* `arbicore/shadow_book.py` — shadow trades without exchange orders
* `arbicore/walk_forward.py` — walk-forward window helper
* Lab API includes shadow snapshot

## Activate

```powershell
git pull origin main
Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force
python scripts\fix_project.py
# expect: apply_server_patches, lab fallback, snapshots ok

$env:ARBICORE_AGENT_LOOP = "1"
$env:ARBICORE_AGENT_PAPER_EXEC = "1"
python server.py
```

Verify: `/api/lab` → JSON · `/agent_lab.js` → script · `/lab` → Ctrl+F5

## Will not be coded

* Unrestricted always-on LIVE
* Guaranteed profits
* Multi-month production proof without running the bot
