# Start ArbiCore with agent observation + paper path (no live orders from agents)
# Run from repo root:
#   .\scripts\start_agent_paper.ps1

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

Write-Host "Pulling latest..." -ForegroundColor Cyan
git pull origin main

Write-Host "Applying agent/lab patches to server.py..." -ForegroundColor Cyan
python scripts\fix_project.py

$env:ARBICORE_AGENT_LOOP = "1"
$env:ARBICORE_AGENT_PAPER_EXEC = "1"
# Optional: queue risk-cleared intents (still no auto live send)
# $env:ARBICORE_AGENT_HANDOFF = "1"

Write-Host "Starting server (agent loop ON, paper exec ON)..." -ForegroundColor Green
Write-Host "Open http://127.0.0.1:5050/lab for Strategy Lab" -ForegroundColor Green
python server.py
