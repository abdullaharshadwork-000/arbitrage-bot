# Web dashboard and API

`server.py` is a small Flask app that drives the engine in `arbitrage_bot.py`
and the `arbicore` package, and serves the dashboard in
`arbitrage-bot-terminal.html`. It is the normal way to run the bot: the CLI
script still works, but only the dashboard shows the risk limits, the startup
check, the alert log and the recovery actions.

| File | Purpose |
|---|---|
| `server.py` | Flask backend — owns the scan thread, exposes it over HTTP |
| `arbitrage-bot-terminal.html` | Dashboard frontend, served by `server.py` |
| `start_live.py` | Launcher that validates `live_config.py` before serving |

## Run

```bash
pip install -r requirements.txt
```

```bash
python server.py
```

Open the URL the banner prints. It tries 5000 first and falls back to 5050,
5055, 8000, 8080 — on Windows, port 5000 is often reserved by Hyper-V/WSL2, so
do not assume 5000.

## Access control

The server binds to `127.0.0.1` only, so nothing off this machine can reach it.
That is not enough on its own: any page open in your browser can also POST to
`http://localhost:5000`, and `POST /api/config` is a route that can turn on real
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
curl -X POST http://localhost:5000/api/pause -H "X-Arbicore-Token: $ARBICORE_TOKEN"
```

This is still a single-user local tool. It has no user accounts, no TLS and no
rate limiting; do not expose it to a network.

## API

| Method | Route | What it does |
|---|---|---|
| GET | `/api/state` | Everything the dashboard polls once a second, plus readiness |
| GET | `/api/readiness` | Whether the current config could legally run, and why not |
| GET | `/api/balances` | Per-exchange balances and their valuation |
| GET | `/api/history` | Trades from `arbicore.db` |
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

- Demo mode uses synthetic prices and no network. Live mode reads real public
  order books through `ccxt`. Neither of those places an order: real execution
  additionally requires `execution_mode = "real"`, live data, credentials, and
  the exact acknowledgement string described in `LIVE_TRADING_GUIDE.md`.
- This is Flask's development server, running one thread for the scan loop. It is
  a local operator console, not a deployment target.
