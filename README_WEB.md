# Web dashboard for arbitrage_bot.py

This adds a live dashboard on top of your existing `arbitrage_bot.py` —
nothing in that file is modified. `server.py` imports its classes
(`PaperWallet`, `DemoFeed`, `LiveFeed`, `find_opportunity`, `log_trade`, ...)
directly and drives them from a small Flask app, so the dashboard shows
the real engine's state instead of a simulation.

## Files added

| File | Purpose |
|---|---|
| `server.py` | Flask backend — runs the bot loop, exposes it over HTTP |
| `arbitrage-bot-terminal.html` | Dashboard frontend, served by `server.py` |

Put both files in the **same folder** as your existing `arbitrage_bot.py`.

## Setup

```bash
pip install flask ccxt
```

(`ccxt` is only actually used if you switch Mode to "live" in the dashboard.)

## Run

```bash
python3 server.py
```

Then open **http://localhost:5000** in your browser.

## What's connected to what

- **Start / Pause / Reset** buttons call the backend, which starts/stops
  the real scan loop (same logic as `main()` in `arbitrage_bot.py`, just
  running as a background thread instead of a blocking `while True`).
- **Settings** (trade size, fee, min profit floor, scan interval, demo
  gap frequency) push straight to the backend's config and apply on the
  next scan.
- **Mode** (demo/live), and the **Exchanges**/**Symbols** chips, rebuild
  the engine on the backend — the dashboard pauses, reconfigures, and
  resets automatically when you change one of these.
- **Export CSV** downloads the actual `trades.csv` the backend is
  writing in this folder — the same file the original CLI script
  produces, not a copy.
- Everything is polled once a second over `GET /api/state`. If the
  backend isn't running, the dashboard shows a red banner instead of
  silently going stale.

## Notes

- Live mode still only reads *public* market data via `ccxt` — no API
  keys, no account, no real trades. Same guarantee as the original
  script.
- This is a local dev server (Flask's built-in one) meant for running
  on your own machine, not for deploying publicly as-is.
