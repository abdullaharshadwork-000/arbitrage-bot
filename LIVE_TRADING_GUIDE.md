# Live trading guide

Nothing here places an order until you have gone through every step. That is
deliberate: the bot has two independent gates, and both have to be opened by
hand.

## 1. The two gates

Real orders require **all** of the following. Miss any one and the bot refuses to
start with a sentence telling you which:

| Setting | Value | Where |
|---|---|---|
| `MODE` | `"live"` | `live_config.py` |
| `EXECUTION_MODE` | `"real"` | `live_config.py` |
| `REAL_TRADING_ENABLED` | `True` | `live_config.py` |
| `REAL_TRADING_ACK` | `"I ACCEPT REAL LOSSES"` | `live_config.py` or `ARBI_REAL_TRADING_ACK` |
| API key + secret | per exchange | environment (preferred) or `live_config.py` |

Two gates exist because one boolean is not a decision. A config file copied from
another machine keeps its flags; the written acknowledgement is the part nobody
copies by accident.

Cross-exchange arbitrage needs credentials on at least **two** exchanges;
triangular needs **one**.

## 2. Where the keys go

Export them rather than typing them into a file:

```bash
export ARBI_BINANCE_API_KEY=...
export ARBI_BINANCE_API_SECRET=...
export ARBI_KUCOIN_API_KEY=...
export ARBI_KUCOIN_API_SECRET=...
export ARBI_KUCOIN_PASSWORD=...      # KuCoin and OKX also need the passphrase
```

The environment always wins over `live_config.py`, so a developer's local file
cannot override the keys an operator set. A key that never touches disk cannot be
committed, backed up by an editor, or read out of a stale copy of the folder.

`live_config.py` is gitignored, which protects the repository and nothing else —
the keys in it are still plaintext on your disk.

## 3. How the API keys must be configured

On the exchange, for every key the bot uses:

- **Trading permission on. Withdrawal permission off**, permanently. If the bot,
  or anything that reaches its keys, cannot move coins off the venue, the worst
  case is bad trades rather than an empty account.
- **IP allowlist** restricted to the machine that runs the bot.
- Ideally a **dedicated sub-account** holding only the capital you are trading,
  so a mistake is bounded by what is in it.

Placeholder values copied from `live_config.example.py` count as no credentials
at all, so a half-filled config is refused at startup rather than mid-trade.

## 4. What has to be true about your accounts

Cross-exchange arbitrage cannot wait for a transfer. A withdrawal takes minutes;
the gap lasts seconds. So before real mode is worth trying:

- Both venues funded, each holding **USDT and the coin** — the bot buys on one
  side and sells on the other simultaneously, which only works if the coin is
  already sitting where the sell happens.
- Enough balance on each venue for the configured trade size plus fees.
- Rebalancing costs (withdrawal fees, transfer time) budgeted, because they come
  out of the same thin margin.

## 5. Suggested sequence

1. Run in **demo** mode and watch the mechanics.
2. Switch `MODE` to `"live"` with `EXECUTION_MODE = "paper"`. This reads real
   order books and simulates fills against them with latency and slippage. Leave
   it running for days, not minutes.
3. Compare the paper results against what the spreads actually were. If paper is
   barely profitable, real will not be — real adds partial fills and rejections.
4. Only then set the two real-trading gates, with a **tiny** trade size (10–20
   USDT) and `MIN_PROFIT_PCT` well above the round-trip fee.
5. Watch the first fills one at a time. Check the exchange's own order history
   against the bot's blotter.
6. Increase size slowly, and only after a stretch with no failed legs.

## 6. What stops the bot on its own

These are enforced, not advice. All of them are visible in the dashboard's Risk
Limits panel, and each one **latches** — the loop stays stopped until you clear
it deliberately.

| Limit | Default | What it does |
|---|---|---|
| `max_daily_loss_usdt` | 50 | Halts for the day once realized losses reach it |
| `max_position_notional_usdt` | 400 | Refuses any single trade above it |
| `max_consecutive_failures` | 3 | Halts after repeated execution failures |
| `max_orders_per_minute` | 20 | Refuses trades once the venues have seen that many orders |
| `halt_on_stranded_position` | on | Halts while unhedged inventory exists |

A refusal is not a halt: a trade blocked for size or order rate shows in the
blotter as `blocked` and the loop keeps scanning. A halt stops the loop and needs
`Resume` in the dashboard, which keeps the day's counters — resuming does not
forgive the loss that triggered it.

Before the first real scan, a startup check verifies clock skew against the
venues, balances, and that every symbol is tradable. Blocking problems prevent
real trading outright.

## 7. When one leg fills and the other does not

This is the expensive failure, and it will happen eventually. The bot:

1. Stops the loop and latches the risk manager.
2. Writes a recovery record to `arbicore.db` with the order id, venue, symbol and
   quantity, so it survives a restart.
3. Shows it in the dashboard with the manual actions.

If the exchange confirmed the fill quantity, the dashboard can market-sell it
back for you. If it did **not**, the recorded quantity is an upper bound and the
bot refuses to sell automatically — selling a size that was never bought either
fails or dumps unrelated inventory. Go and look at the order on the exchange.

Clearing the stranded latch sells nothing. It only tells the bot you have dealt
with the position; doing it while the coin is still sitting there is how the next
run starts trading around inventory it does not know about.

## 8. Operational hygiene

- Set an alert webhook. Without one, a 3am halt goes unnoticed until someone
  opens the dashboard.
- The dashboard is bound to `127.0.0.1` and every mutating request needs the
  session token printed at startup. It has no user accounts and no TLS — do not
  expose it. See `README_WEB.md`.
- Keep `trades.csv` and `arbicore.db`. They are how you find out whether the
  strategy actually made money after fees, rather than whether it felt like it.

## 9. Risk warning

Cross-exchange arbitrage is not a stable profit machine. Real spreads are small
and close in seconds, and firms with servers inside the exchange's data centre
get there first. Execution risk, fees, balance mismatches and failed fills can
erase the expected gain of a trade that looked good when it was found.

Treat this as a carefully controlled experiment with money you can afford to
lose, not as income.
