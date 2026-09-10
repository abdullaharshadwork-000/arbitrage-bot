# Trading system architecture and release gates

## Current boundary

The application has existing guarded real-arbitrage execution. The new trend
strategy is **live-data paper only**, including when the market chart displays a
BUY candidate. Native-order workbenches for Binance Testnet, OKX Demo and Bybit
Testnet are separate from the production engine. No authenticated qualification
is inferred from unit tests or a successful balance read. KuCoin has no verified
equivalent qualification environment in this implementation.

This is not yet a fully qualified multi-user professional trading service.
Settings, sessions, credentials and persisted accounts are user-scoped, but the
server still has **one active trading-worker owner**, not an independent running
worker per user. Do not deploy it as a concurrent managed-funds service.

## Data flow

```text
Public exchange candles/books -> validation -> versioned strategy decision
                                                   |
Private account/fills -> ledger -> risk checks ------+
                                                   v
                                   durable order intent -> exchange adapter
                                                   |
                                   fills + native protection reconciliation
                                                   |
                                   ledger/risk update -> dashboard + audit
```

The dashboard reads these facts. It never manufactures balances, writes fills,
or authorizes an order merely because a chart says BUY.

| Layer | Existing implementation | Remaining production work for trend trading |
| --- | --- | --- |
| Market data | `arbicore/market.py`, `feed.py`, `streaming.py`; public chart fetches without API keys, rejects malformed/gapped/stale candles | Sustained venue-specific latency/outage qualification; bounded worker scheduling under many users |
| Strategy | `signals.py`; EMA9/21, RSI14, ATR14, five/ten-minute momentum, volume; completed 1m candles | Walk-forward evaluation across market regimes, realistic net costs and extended paper observation |
| Risk | `risk.py`, `safety.py`, `config.py`; loss/exposure/rate limits, latched halts, paper signal sizing | Unified account equity loss including unrealized P&L and cash-flow adjustment; independent account workers |
| Accounting | Decimal ledger, paper snapshots, durable order/fill records | Reconcile every supported live trend entry, fee asset, partial exit and residual against venue evidence |
| Execution | Existing arbitrage engine; separate signal demo adapters with durable intents | Connect only individually qualified adapters; enforce account identity across key rotation for every venue |
| Protection | Binance Testnet OTOCO and OKX Demo attached OCO reconciliation; Bybit partial workbench | Authenticated native-stop coverage, dynamic exit cancel/fill races, restart reconciliation, residual liquidation or operator lock |
| Dashboard | Candles, EMA, volume, trend reasoning, saved account target/loss status, existing trade/P&L views | Verified live trend position/stop/target states once execution is qualified; external alert delivery and operational runbooks |

## Implemented market dashboard

`GET /api/market/overview?exchange=binance&symbol=BTC%2FUSDT&timeframe=1m`
requires an authenticated session. Enum-validated exchanges are Binance, KuCoin,
OKX and Bybit; symbols come from the application's existing market list. Chart
intervals are 1m, 5m and 15m. Decision rules always use completed **1m** candles,
so changing the visual interval does not silently change the trading strategy.

The selected exchange must provide that market. Failures show unavailable data;
there is no synthetic or different-exchange fallback. Shared caches contain only
public candles. Private risk and position context is attached per authenticated
request and never claims or changes the worker owner. Corrupt risk amounts are
shown as unavailable with a blocked status, not a reassuring zero.

The browser requests updates after a five-second delay between responses, only
while the terminal/tab is visible. This is REST polling, not tick-by-tick streaming.
It updates existing chart series without replacing the canvas on every poll,
retains the viewport, and suppresses obsolete responses after a selection change.
Failed/expired feeds show WAIT and mark retained chart history stale. Profit Trend
continues to show realized trading results, separately from market prices.

BUY CANDIDATE is a rule match, not an execution permission. EXIT applies only to
the account's matching tracked long paper position; bearish analysis without a
position means WAIT, not a naked short. The chart always returns
`execution_allowed: false`. Its saved target/readiness summary is not a promise
that staged, unsaved form values are active.

The displayed daily loss remainder is based on **recorded bot realized P&L**.
It excludes unrealized changes, deposits/withdrawals, manual exchange activity,
and fees that have not yet been reconciled. It is not an exchange-wide drawdown
guarantee. Paper stops are simulated; no real protective coverage is advertised.

## Required production entry contract

Before any future real trend entry, one atomic account-level reservation must
verify all of the following against a versioned decision and configuration:

1. Fresh, contiguous market data and a non-expired closed-candle signal.
2. Healthy authenticated account, valid spot market limits and permissions,
   accurate free balances, no unresolved intent or unprotected inventory.
3. Native protection supported **and qualified for this venue/environment**.
4. Quantity rounded down to the venue's step, constrained by free cash, order
   notional, account exposure and risk budget. If below minimum, skip the trade;
   never increase size to force acceptance.
5. Estimated stop risk includes both-side fees and stressed exit slippage.
   `quantity <= risk_budget / (entry - stop + estimated_cost_per_unit)` for a
   long position. The current paper strategy uses a 0.25% equity risk cap;
   that is a test rule, not a recommended or loss-guaranteeing allocation.
6. Latched daily-loss/kill-switch state, cooldown and order/trade-rate limits
   permit entry. UI toggles cannot reset these checks.
7. Durable intent with a unique client ID is committed before sending. A timeout
   means unknown outcome and reconciliation, not an automatic resubmission.

## Protective lifecycle required before release

```text
INTENT -> ENTRY_UNKNOWN / ENTRY_OPEN -> FILLED -> PROTECTED
                                         |          |
                                  RECONCILE_ONLY <---+-- EXIT_PENDING
                                         |                 |
                                   OPERATOR_LOCK       CLOSED / RESIDUAL
```

Actual states are adapter-specific. Do not mark PROTECTED from the submission
response alone: verify child identity, status, stop/target triggers and coverage
of the actual net quantity after fees. Partial fills, canceled children, base
fees, dust and externally changed orders must remain explicit states.

For reversal/trailing/time exits, reconcile before modifying protection. A stop
can fill while cancellation is in flight. Re-read orders and individual fills,
recalculate remaining inventory, and never issue a second full-size exit based
on an old balance. Persist every transition. Restart begins in reconciliation
mode and cannot send new entries until exposure/protection is known.

A daily-loss halt blocks new entries but must continue managing exits/protection.
An emergency stop should not blindly cancel the only protective orders. A hard
loss cap is not guaranteed: gaps, fees, liquidity, outages and exchange failures
can exceed the planned stop risk.

## Release acceptance

- Mock tests cover duplicates, timeouts before/after acknowledgement, out-of-order
  events, concurrent reconciliations, partial fills, fees, canceled stops and
  restart recovery without claiming exchange qualification.
- Dedicated testnet/demo credentials then demonstrate real submissions, fills,
  protective triggers, and recovery. Production keys are never reused for this.
- Inspect ledger reconciliation and externally valued fees/residuals; confirm no
  unsupported venue can silently fall back to real orders.
- Run extended live-market paper tests with fees, slippage and losses; review
  drawdown, exposure time, rejected/stale signals and execution latency, not only
  win rate. Define acceptance thresholds before evaluation, not after observing it.
- Only after these gates should a separately approved, tightly capped production
  trial be considered. Neither this architecture nor successful tests guarantee
  profit, uninterrupted operation, or safety for real funds.

See [SIGNAL_TRADING.md](SIGNAL_TRADING.md) for current strategy rules, adapter
commands, exact limitations and exchange documentation sources.
