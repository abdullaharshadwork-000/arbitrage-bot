# Automatic trend signals — paper preview

Choose **Trading Terminal → Strategy → Automatic trend signals**. This selects
live data, paper execution and one venue. No exchange credentials are needed for
public candles. Start Engine to monitor; the signal panel explains each decision.
The signal wallet is separate from the arbitrage wallet and initially contains
20,000 virtual USDT. Closed signal results are separated from historical
arbitrage trades in the dashboard statistics and trade-history views.

## Rules implemented

- Observe exchange bid/ask quotes every approximately five seconds, plus request
  80 one-minute OHLCV candles (cached for 15 seconds). Network time adds to the
  scan duration. Only completed, contiguous, fresh candles can authorize entry.
- Entry: positive five- and ten-minute momentum, at least seven rising closes
  out of ten, EMA9 above EMA21, Wilder RSI14 between 50 and 78, and latest volume
  at least 1.1 times the preceding twenty-candle average. A continuous rise can
  be rejected as overbought; waiting is an intentional decision.
- Long-only Spot positions in USDT pairs; one position per account. Size is
  capped by configured trade size, maximum position, free cash, 0.25% of paper
  equity at estimated stop risk, and remaining daily loss budget.
- Stop distance: twice Wilder ATR14 / price, bounded to 0.5–3%. Target: twice
  stop distance plus an estimated fee allowance. A trailing stop activates after
  a one-stop-distance gain. Reversal or a thirty-minute maximum hold also exits.
- Exit checks use current quotes, even when candle data is unavailable. No
  quote/book means no simulated fill. Stops cannot guarantee a loss ceiling.
- Five-minute re-entry cooldown, candle deduplication and hourly entry cap.
  Daily loss accounting uses UTC. Daily equity loss triggers an attempted exit
  and blocks entries. It does not fabricate an exit when liquidity is absent.
- Fees are explicitly simulated using the configured fee, not advertised as
  authenticated account fees. Fills use a freshly fetched order book following
  a simulated delay, with precision, liquidity and exchange limit checks.
- Positions, holdings and cooldowns persist. Failed persistence stops the engine
  and restores uncommitted paper state. Pausing/server downtime stops monitoring;
  there are no exchange-side protective orders in paper mode.

## Boundaries

Real-money and testnet order execution are deliberately blocked for this new
strategy. It needs strategy evaluation, live paper observation and a separately
tested protective-order lifecycle before production use. Profits and ideal entry
or exit timing are not guaranteed. Arbitrage execution is otherwise unchanged.

This implementation calculates its own indicators from exchange data. It does
**not** scrape TradingView, use its proprietary indicators, or receive its alerts.
TradingView integration is a separate webhook deployment/configuration task:
https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/

## Exchange qualification workbench

The terminal lists Binance, KuCoin, OKX and Bybit separately. All four use the
same live-data paper signal strategy, but native order semantics are not assumed
interchangeable. Production signal execution remains blocked for every venue.

| Venue | Non-production environment | Implemented stage |
| --- | --- | --- |
| Binance | Spot Testnet | Native OTOCO FOK entry; exact account-trade/commission reconciliation; account-bound restart recovery (mock-tested) |
| OKX | Demo Trading | Dedicated attached OCO submission and entry/triggered-child/fill reconciliation (mock-tested) |
| Bybit | Testnet | Dedicated attached TP/SL submission and entry-fill reconciliation; protective-child lineage remains unverified |
| KuCoin | No verified replacement sandbox | Live-market paper testing; no silent fallback to real orders |

Binance's tool requires explicit `--submit --ack "PLACE TESTNET ORDERS"`; its
default builds a read-only plan. It accepts only dedicated
`BINANCE_TESTNET_API_KEY` / `BINANCE_TESTNET_API_SECRET` environment variables,
caps an entry at 25 USDT and requires a current qualified signal. It never reads
the website's production keys. Inspect usage with:

```powershell
python -m arbicore.signal_testnet --help
python -m arbicore.signal_demo --exchange okx
python -m arbicore.signal_demo --exchange bybit
python -m arbicore.okx_demo --help
python -m arbicore.bybit_demo --help
```

Read-only diagnostics require `OKX_DEMO_API_KEY`, `OKX_DEMO_API_SECRET`, and
`OKX_DEMO_API_PASSPHRASE`, or `BYBIT_TESTNET_API_KEY` and
`BYBIT_TESTNET_API_SECRET`. Keep these local; never paste secrets into reports.
OKX's simulation header and Bybit/Binance testnet destinations are checked before
requests. Region-specific endpoint substitutions are not silently accepted.

All three tools persist intent before submission and block another unresolved
entry in the same journal account slot. Both the application and SDK POST retries
are disabled. A timeout is queried by client identity, never blindly retried.
Read-only restart recovery commands:

```powershell
python -m arbicore.signal_testnet --reconcile-active
python -m arbicore.okx_demo --resume
python -m arbicore.bybit_demo --resume
```

Binance and OKX reconcile individual authenticated fills and fees, rather than
guessing a filled quantity or multiplying a rounded average price. Missing or
duplicate fills, malformed values, incomplete pagination, changes during a
snapshot, partial FOK entries, and regressing cumulative fills block completion.
Already-observed order fills remain pinned even when the subsequent fee/history
request fails. Journal updates use compare-and-set guards, so a delayed recovery
check cannot overwrite newer committed evidence or unlock an unresolved account.
Residual coins are not discarded as dust. External fee currencies are not given
invented USDT values. These checks have only been exercised with local fixtures.

Binance now uses a hashed authenticated Spot account UID, so changing keys on
the same account does not create a new slot. The tool migrates the current key's
older fingerprint-owned journal rows; historical rows for already-deleted keys
cannot be linked automatically. OKX/Bybit tools still use key-scoped identities.
Do not operate multiple keys or journals for the same account, or delete a journal
to bypass unresolved records. Keep the journal on persistent local storage.

OKX's tool requires `--submit --plan <fresh-plan.json>` and
`--ack "PLACE OKX DEMO ORDERS"`. Bybit requires the same submit/plan flags and
`--ack "PLACE TESTNET ORDERS"`. These are operator qualification tools: plans are
fresh, manually prepared spot buys, capped at 25 virtual USDT and at most 10
seconds old, not automated strategy entries. JSON fields are `symbol`, `quantity`,
`entry`, `stop`, `target`, and `created_at` (Unix seconds). OKX symbols use
`BTC/USDT`; Bybit uses `BTCUSDT`. Confirm market precision and minimum limits
before preparing a plan. Do not put credentials in the plan.

OKX observes the attached algo by its client ID and verifies its triggers and
spawned exit order IDs. Bybit never infers Spot protective-child ownership from
matching price, symbol, or size: after a fill it remains
`child_linkage_unverified`, even if candidate stop orders are visible. This is a
blocking limitation, not a successful protective-order qualification.

No automated cancel-and-replace or forced residual exit is implemented here:
existing exchange protection is not removed on app exit. Protection observations
do not guarantee stop execution, execution price, or loss limits.

These are development qualification tools, not a completed live trading rollout.
Outstanding: actual authenticated order trials for every available demo venue,
Bybit child-order verification, automatic residual/dynamic-exit recovery, and
sustained per-venue live-market paper and demo runs before enabling any signal
strategy with real funds. Unit tests and historical replay cannot satisfy these
requirements. Production signal execution remains blocked in the web engine.

Official references:

- [Binance native order lists](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/trade)
- [OKX demo trading and attached orders](https://www.okx.com/docs-v5/en/)
- [Bybit Spot order parameters](https://bybit-exchange.github.io/docs/v5/order/create-order)
- [KuCoin sandbox suspension notice](https://www.kucoin.com/announcement/kucoin-will-delist-the-sandbox-mode-0629)

## Offline historical stress replay

```powershell
python -m arbicore.signal_validation candles.json --help
python -m arbicore.signal_validation candles.json
```

Input is a JSON array of consecutive closed 1-minute OHLCV rows. Replay uses
previous completed candles only, next-bar entries, adverse slippage, entry/exit
fees, rounded amounts, loss caps and cooldowns. A bar touching both stop and
target is resolved against the strategy. It reports profits, losses and maximum
drawdown; exit liquidity is assumed, because candle data does not contain depth.
Historical replay never grants production readiness or demonstrates future profit.
