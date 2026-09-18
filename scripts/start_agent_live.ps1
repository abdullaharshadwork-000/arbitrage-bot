# Agent LIVE path – REAL money possible
# Requirements BEFORE running:
#   1. Dashboard: mode=live, execution=real, real trading ack typed
#   2. Exchange API keys connected with trade permission (no withdraw)
#   3. You understand canary starts at 5% of approved notional
#
# Run from repo root:
#   .\scripts\start_agent_live.ps1

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

Write-Host "WARNING: Agent live execution can place REAL exchange orders." -ForegroundColor Yellow
Write-Host "Gates still apply: LiveModeGuard + Risk Kernel + canary fraction." -ForegroundColor Yellow

git pull origin main
python scripts\fix_project.py

$env:ARBICORE_AGENT_LOOP = "1"
$env:ARBICORE_AGENT_PAPER_EXEC = "0"
$env:ARBICORE_AGENT_HANDOFF = "1"
$env:ARBICORE_AGENT_LIVE_EXEC = "1"

Write-Host "Flags: LOOP=1 HANDOFF=1 LIVE_EXEC=1" -ForegroundColor Green
Write-Host "You must still enable live+real in the dashboard and register place_fn via wire." -ForegroundColor Cyan
Write-Host "See AGENTIC.md section 'Agent live orders'." -ForegroundColor Cyan
python server.py
