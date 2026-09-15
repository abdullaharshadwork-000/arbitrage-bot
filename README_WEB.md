# Web dashboard and API

`server.py` is a small Flask app that drives the engine in `arbitrage_bot.py`
and the `arbicore` package, and serves the dashboard in
`dashboard-pro.html`. It is the normal way to run the bot: the CLI
script still works, but only the dashboard shows the risk limits, the startup
check, the alert log and the recovery actions.

| File | Purpose |
|---|---|
| `server.py` | Flask backend — owns the scan thread, exposes it over HTTP |
| `dashboard-pro.html` | Role-aware dashboard frontend served at `/` |
| `arbitrage-bot-terminal.html` | Legacy operations terminal using the same APIs |
| `start_live.py` | Launcher that validates `live_config.py` before serving |

## Run

From the `arbitrage-bot` directory, install Python and browser dependencies:

```powershell
python -m pip install -r requirements.txt
npm.cmd ci --ignore-scripts
```

```bash
python server.py
```

On this Windows workstation, the `python` app-execution alias is inaccessible.
The verified interpreter can be invoked directly from PowerShell:

```powershell
& "$env:LOCALAPPDATA\Python\pythoncore-3.14-64\python.exe" server.py
```

Use the same interpreter path with `-m pip` or `-m pytest` if needed. Other
machines should use their own installed Python or virtual environment path.

Open the URL the banner prints. It tries 5000 first and falls back to 5050,
5055, 8000, 8080 — on Windows, port 5000 is often reserved by Hyper-V/WSL2, so
do not assume 5000.

Opening the website does not start the engine. Keep **Paper** selected for
evaluation; selecting real funds is not evidence that a strategy is qualified.
The new trend strategy remains blocked for real-money execution.

The Trading Terminal includes **Live Market & Decision**: public exchange
candlesticks, EMA overlays, volume, closed-candle suggestions, and the signed-in
account's saved risk status. Changing this chart's exchange/pair does not change
the engine configuration or submit an order. See [the architecture and release
gates](LIVE_SYSTEM_ARCHITECTURE.md).

Run the browser suite safely against a fresh temporary database, with exchange
network access disabled:

```powershell
python tests/run_browser_smoke.py
```

This test does not load `live_config.py` or use your database/API keys. It tests
the market UI using deterministic fixtures, not authenticated live execution.

## Access control

Dashboard profit and win rate use closed trades for the signed-in account's
saved mode; tutorial, live-data paper, signal paper, testnet and real funds are
not mixed. Today's Profit uses UTC midnight. A +100 gain followed by a -16 loss
produces 84 net profit. A second +84 gain instead produces 184. Breakeven trades
count in the denominator but are not wins. Pending/partial/invalid records do
not inflate performance. Older records without identifiable mode are preserved
in history but excluded from scoped metrics. Open inventory price movement is
separate from realized trade profit; it is not silently relabeled as a closed
trade loss or gain. The realized-loss sizing guard refuses new exposure when
its remaining budget is exhausted or invalid; this cannot guarantee profits
or prevent losses beyond a limit during market gaps or exchange failures.

On a new or migrated database, startup idempotently creates the administrator
configured privately with `ARBICORE_ADMIN_USERNAME`,
`ARBICORE_ADMIN_PASSWORD`, and `ARBICORE_ADMIN_EMAIL`. Never publish those
values in the UI, documentation distributed to traders, or client-side code.
Passwords are stored as
Werkzeug hashes, never plaintext. Re-running database initialization preserves
the existing account and does not reset its password. Trader registrations are
stored in the same SQLite `users` table, and roles are read from the database;
the browser cannot promote itself by submitting a role.

The server binds to `127.0.0.1` only, so nothing off this machine can reach it.
That is not enough on its own: any page open in your browser can also POST to
a loopback service, and `POST /api/config` is a route that can turn on real
execution. So every mutating request must satisfy two checks:

- **Same origin.** The `Origin` header, when present, must equal the server's own
  origin. `SameSite` cookies are no help here — the browser treats every port on
  `localhost` as one site — so the comparison is against the `Host` the request
  actually arrived on, which a page cannot forge.
- **Session token.** A random token is generated at startup and printed in the
  banner. The dashboard receives it as an `HttpOnly; SameSite=Strict` cookie on
  its first `GET`, so in a browser there is nothing to do. Scripts pass it as an
  `X-Arbicore-Token` header.

`GET` requests need no token. A rejected request answers 403 and deliberately
does *not* set the cookie, so the first probe cannot collect the credential it
was missing.

The token rotates on every restart. Pin it if you are scripting against the API:

```bash
ARBICORE_TOKEN=$(python -c "import secrets;print(secrets.token_urlsafe(24))") python server.py
```

```bash
curl -X POST http://127.0.0.1:PORT/api/pause -H "X-Arbicore-Token: $ARBICORE_TOKEN"
```

Production hardening, encrypted credential persistence, testnet qualification,
health monitoring, recovery, and deployment requirements are documented in
[`PRODUCTION.md`](PRODUCTION.md). Keep the service on loopback unless it is behind
a maintained HTTPS reverse proxy.

## API

| Method | Route | What it does |
|---|---|---|
| GET | `/api/state` | Everything the dashboard polls once a second, plus readiness |
| GET | `/api/readiness` | Whether the current config could legally run, and why not |
| GET | `/api/balances` | Per-exchange balances and their valuation |
| GET | `/api/history` | Trades from `arbicore.db` |
| GET | `/api/intelligence` | Market regime, next-scan forecasts, confidence decisions, and live-data strategy evidence |
| GET | `/api/risk` | Risk-manager snapshot (counters, halt state, stranded inventory) |
| GET | `/api/recovery` | Open manual-recovery records |
| GET | `/api/trades.csv` | The real `trades.csv` this run is appending to |
| POST | `/api/start` `/api/pause` `/api/emergency-stop` `/api/reset` | Loop control |
| POST | `/api/config` | Change settings; structural changes rebuild the engine |
| POST | `/api/test-connection` | Probe each configured exchange |
| POST | `/api/risk/resume` | Clear a latched halt — needs `RESUME_AFTER_HALT` |
| POST | `/api/risk/stranded/clear` | Mark stranded inventory as dealt with — needs `STRANDED_POSITION_UNWOUND` |
| POST | `/api/recovery/close` | Market-sell a stranded position — needs `CLOSE_UNHEDGED_POSITION` |

The confirmation strings are required in the JSON body. They exist because each
of these actions is one an operator can only mean on purpose: resuming keeps the
day's loss counters, and clearing the stranded latch sells nothing — it only
tells the bot to stop refusing to run.

## What the dashboard shows

- **Blotter** — completed trades, scans that found nothing (`miss`), and risk
  vetoes (`blocked`, yellow, with the limit that stopped it). Repeated vetoes for
  one reason collapse into a single counted row so they cannot bury the trades.
- **Risk Limits** — realized P&L today against the daily loss budget, win/loss
  counts, consecutive failures, orders in the last minute, drawdown, stranded
  inventory, and the last limit that fired. `Resume` and `Clear stranded` appear
  here only when they are relevant.
- **Startup check** — every reason the current configuration cannot trade for
  real, with blocking reasons marked.
- **Alerts** — the halt/failure notifications the bot raised, and a note when no
  webhook is configured, because in that case they are local only.
- **Balances, feed health, chart, export CSV** — as before.

## Data it writes

| Path | Contents |
|---|---|
| `arbicore.db` | SQLite trade history; migrates in place from older schemas |
| `trades.csv` | The same CSV the CLI script writes |

Set `ARBICORE_DB` to point the database somewhere else — useful for a second
instance or a test run, so it cannot touch your real history.

## Notes

### Live account exposure guard

Real execution (production and sandbox) checks authenticated spot balances on
every scan and again before each new route. It includes locked funds and coins
outside the selected trading symbols on the selected venues. Missing venues,
unpriced holdings, invalid balances, or a valuation taking over 30 seconds block
entries; they never replace the last valid equity with a misleading zero.

The per-user **Maximum Live Coin Exposure (%)** setting defaults to 50%. New
orders are sized within remaining non-USDT inventory capacity, reserving a full
buy leg plus fee/slippage headroom without assuming the sell leg succeeds.
Exposure above the limit blocks entries, but does not liquidate existing coins
or block the separate manual recovery workflow.

The existing equity peak drawdown guard includes unrealized losses and now
latches on balance updates even without trade opportunities. Remaining drawdown
room also reduces new order sizing. Peak equity persists across restarts; gains
do not automatically clear a halt. Withdrawals or other external account changes
can trigger this guard and require reconciliation. It does not guarantee exit
prices or prevent losses on inventory still held, especially while paused or
disconnected. This covers spot wallets, not futures, margin, or Earn balances.
Authenticated exchange qualification and existing production restrictions remain
required; these changes do not enable production trend-signal orders.

- Demo mode uses synthetic prices and no network. Live mode reads real public
  order books through `ccxt`. Neither of those places an order: real execution
  additionally requires `execution_mode = "real"`, live data, credentials, and
  the exact acknowledgement string described in `LIVE_TRADING_GUIDE.md`.
- This is Flask's development server, running one thread for the scan loop. It is
  a local operator console, not a deployment target.
