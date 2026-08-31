# ArbiCore — Route-Aware Arbitrage Engine

A Python bot that watches several exchanges, finds cross-exchange and
single-exchange triangular price gaps, and executes them — as paper trades
against real order books by default, or with real money only after you open two
deliberate gates.

It ships with a browser dashboard, latching risk limits, order reconciliation,
and a recovery path for the trade that half-filled.

---

## 1. Realistic expectations (read this first)

The viral post that inspired this claimed $68 → $750,000. That is not how
arbitrage works:

- Real gaps are **tiny** — usually 0.05% to 0.5%. A $200 trade at 0.3% earns
  about **$0.60**, before you lose some of it to slippage.
- Gaps close in seconds. Firms with servers inside the exchange's data centre
  compete for the same ones.
- Paper mode is honest here on purpose: it fills against real book depth, applies
  latency, and rechecks the spread after the fill. If paper barely profits, real
  will not.

Treat any real-money run as an experiment with money you can afford to lose.

## 2. Setup

Install Python 3.10+ ([python.org](https://www.python.org/downloads/); on Windows
tick **"Add Python to PATH"**), then:

```bash
pip install -r requirements.txt
```

## 3. Run it

The dashboard is the normal way — it is the only place the risk limits, startup
check, alerts and recovery actions are visible:

```bash
python server.py
```

Open the URL it prints. It tries port 5000 and falls back to 5050, 5055, 8000,
8080, because Windows often reserves 5000.

The original single-file CLI still works:

```bash
python arbitrage_bot.py
```

## 4. Two independent switches

Data source and execution are separate, and that is the whole safety model:

| | `EXECUTION_MODE = "paper"` | `EXECUTION_MODE = "real"` |
|---|---|---|
| `MODE = "demo"` | Synthetic prices, no network. Start here. | Refused |
| `MODE = "live"` | Real public order books, simulated fills. Run this for days. | Real orders — needs everything in `LIVE_TRADING_GUIDE.md` |

Live *data* needs no account and no API keys; price feeds are public. Real
*execution* additionally needs `REAL_TRADING_ENABLED = True`, the exact
acknowledgement string `REAL_TRADING_ACK`, and trade-only API keys with
withdrawals disabled. Read `LIVE_TRADING_GUIDE.md` before touching that column.

## 5. What each scan does

1. Fetch order books for every configured symbol on every configured exchange.
2. Find the lowest ask and the highest bid, or walk triangular routes within one
   venue.
3. Price the trade against **book depth**, not just the top level, and subtract
   both fees plus expected slippage.
4. Ask the risk manager whether this trade is allowed at all.
5. Execute it — simulated or real — then reconcile what actually filled against
   what was requested.
6. Record it in `trades.csv` and `arbicore.db`, and publish it to the dashboard.

## 6. Settings

Everything lives in one CONFIG block at the top of `arbitrage_bot.py`, and the
dashboard can change most of it while the bot runs (structural changes rebuild
the engine between scans, never mid-trade).

| Setting | What it does | Default |
|---|---|---|
| `MODE` | `"demo"` (fake prices) or `"live"` (real prices) | `"demo"` |
| `EXECUTION_MODE` | `"paper"` (simulated fills) or `"real"` (money) | `"paper"` |
| `TRADING_STRATEGY` | `"cross_exchange"` or `"triangular"` | `"cross_exchange"` |
| `EXCHANGES` | Which venues to watch | binance, kucoin, okx, bybit |
| `SYMBOLS` | Which pairs to scan | BTC, ETH, SOL, XRP, DOGE vs USDT |
| `TRADE_SIZE_USDT` | Size per trade | 200 |
| `TAKER_FEE` | Fallback fee when the venue does not report one | 0.001 |
| `MIN_PROFIT_PCT` | Net floor after both fees | 0.15 |
| `MAX_SLIPPAGE_PCT` | Tolerated slippage per leg | 0.25 |
| `CHECK_INTERVAL` | Seconds between scans | 5 |

Risk limits (`MAX_DAILY_LOSS_USDT`, `MAX_POSITION_NOTIONAL_USDT`,
`MAX_CONSECUTIVE_FAILURES`, `MAX_ORDERS_PER_MINUTE`) are documented in
`LIVE_TRADING_GUIDE.md` — they apply in paper mode too, so you can watch them
fire before they matter.

## 7. Reading the output

```
[14:32:10] OPPORTUNITY ETH/USDT  buy kucoin @ 3,499.86 -> sell bybit @ 3,518.35 | net +0.33% = $+0.65
```

- **buy kucoin @ 3,499.86** — cheapest place to buy right now
- **sell bybit @ 3,518.35** — most expensive place to sell it
- **net +0.33%** — the gap after both fees
- **= $+0.65** — profit on a $200 trade

In the dashboard's blotter, a yellow `blocked` row is not a missed gap: it is one
the bot found and refused, with the limit that stopped it. Repeats of the same
refusal collapse into one counted row.

## 8. Safety machinery

- **Latching risk limits** — daily loss, per-position notional, consecutive
  failures, orders per minute. A halt stops the loop until you resume it, and
  resuming keeps the day's counters.
- **Startup check** — clock skew against each venue, balances, tradable symbols.
  Blocking problems prevent real trading.
- **Order reconciliation** — every leg's fill is confirmed from the exchange
  rather than assumed from the request.
- **Stranded position recovery** — if one leg fills and the other fails, the bot
  halts, persists a recovery record, and offers a manual close. An unconfirmed
  fill quantity is never sold automatically.
- **Alerts** — halts and failures can be pushed to a webhook.
- **Local access guard** — the dashboard binds to `127.0.0.1` and every mutating
  request needs the session token from the startup banner. See `README_WEB.md`.

## 9. What is in the folder

| Path | Purpose |
|---|---|
| `arbitrage_bot.py` | Engine: feeds, paper wallet, real execution, strategies |
| `arbicore/` | The parts worth testing on their own: `money`, `books`, `orders`, `risk`, `simulator`, `reconcile`, `ledger`, `alerts`, `rebalance`, `config`, `feed` |
| `server.py` | Flask backend and HTTP API |
| `arbitrage-bot-terminal.html` | Dashboard |
| `start_live.py` | Launcher that validates the real-trading config first |
| `live_config.example.py` | Template for `live_config.py` (gitignored) |
| `tests/` | 291 tests |
| `arbicore.db` / `trades.csv` | Trade history (created on first run) |
| `LIVE_TRADING_GUIDE.md` | Everything required before real orders |
| `README_WEB.md` | Dashboard, API routes, access control |

## 10. Tests

```bash
python -m unittest discover -s tests -t .
```

They run in a few seconds and touch no network: exchange clients are stubbed, and
the database and CSV are redirected to a temporary directory, so your own history
is never written to.

## 11. Ideas to extend it

- More venues (ccxt supports 100+) or more symbols
- Measure how long each gap survives, to see what you are competing with
- A rebalance simulator with realistic transfer delays and fees
- WebSocket feeds instead of REST polling, which is where the real latency goes
