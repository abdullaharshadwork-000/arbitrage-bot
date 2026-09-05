# ArbiCore production runbook

## Live execution controls enforced by this build

- Live orders use price-bounded `limit` orders with `FOK` (fill-or-kill), using
  the worst order-book level consumed by the requested quantity. They cannot
  remain resting or execute beyond the approved depth price.
- Three consecutive empty, malformed, stale, or slow quote snapshots latch an
  execution halt that requires explicit operator resume.
- Real order size can only move downward from the configured value. It is
  capped by free quote balance and the remaining daily loss budget.
- A rolling expected-versus-realized edge check halts consistently degraded
  execution.
- Order intents and status transitions are persisted and exposed, per owner,
  at `GET /api/orders`.
- Startup reconciliation, ambiguous-order recovery by client ID, partial-fill
  handling, stranded-position persistence, drawdown/daily-loss limits, order
  rate limits, and the emergency stop remain mandatory.

These controls reduce execution risk; they cannot guarantee profit or make a
networked trading system risk-free.

> Production launch remains blocked until the external requirements below are
> completed. Passing local tests is not regulatory, security, or profitability
> approval.

## Launch blockers outside this repository

- Provision managed PostgreSQL, migrate SQLite data, and test point-in-time restore.
- Run each customer engine in an isolated supervised worker/container; the local
  build intentionally permits only one active owner.
- Configure HTTPS, reverse proxy, firewall, stable outbound IP, DNS, and secret manager.
- Connect an email/MFA provider and verify password-reset and account-recovery delivery.
- Connect alert webhooks and 24/7 uptime/error monitoring with an incident owner.
- Obtain legal review of `LEGAL.md`, supported jurisdictions, custody/data flows,
  taxation language, and exchange terms.
- Backtest with historical order books, then complete paper and Testnet forward
  testing. No test can guarantee future profit.

Real trading is fail-closed. Passing unit tests does not prove profitability,
and no configuration removes market, liquidity, software, or exchange risk.

## Required environment

Generate independent random values; never commit them:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Set the first value as `ARBICORE_SESSION_SECRET` and the second as
`ARBICORE_MASTER_KEY`. Also set:

- `ARBICORE_PRODUCTION=1`
- `ARBICORE_ADMIN_PASSWORD` to a unique password (the development default is refused)
- `ARBICORE_HTTPS=1` when HTTPS terminates at the application or trusted proxy
- `ARBICORE_REQUIRED_TESTNET_CYCLES` (default `100`)
- `ARBICORE_PASSWORD_RESET_WEBHOOK` to a private HTTPS service that accepts
  password-reset delivery events; tokens expire after 15 minutes and are stored
  only as hashes in ArbiCore
- `ALERT_WEBHOOK` or the alert variable documented in `live_config.example.py`

Install pinned dependencies with `python -m pip install -r requirements.txt`.
`python server.py` uses Waitress automatically. Keep the application bound to
loopback and put a maintained HTTPS reverse proxy in front of it if remote access
is required. Do not expose the Flask development server.

## Account isolation and credentials

Browser authentication uses signed, HttpOnly, SameSite cookies. One user owns the
single trading worker at a time; other traders cannot read its balances, recovery
records, exchange diagnostics, or operate its controls. Administrators retain
platform oversight. API keys are encrypted with Fernet before database storage.
Without `ARBICORE_MASTER_KEY`, the UI explicitly falls back to memory-only keys.
Sustained failed logins are temporarily locked, users can revoke other sessions,
and disabling MFA requires both the current password and an authenticator or
one-time recovery code.

Rotate a key by pausing the engine, disconnecting Binance, revoking the old key at
Binance, and connecting the replacement. Keep withdrawals and transfers disabled;
allow only Reading and Spot Trading, restrict the source IP, and allowlist only
`BTCUSDT`, `ETHBTC`, and `ETHUSDT` for the triangular strategy.

## Qualification and activation

Production readiness requires a completed Binance Testnet soak run with the
configured number of successful cycles. Set `ARBICORE_TESTNET_FAILURE_EVERY` to a
positive number only during a sandbox soak to verify that the worker survives
injected scan failures. The setting has no effect in production mode.

Before production activation, confirm all readiness stages are green:

1. public market data;
2. authenticated account access;
3. Binance's non-executing order-test endpoint;
4. free Spot USDT and exposure limits;
5. startup reconciliation with no open/ambiguous orders or stranded inventory;
6. completed testnet qualification.

The Start action still requires the exact `I ACCEPT REAL LOSSES` acknowledgement.

## Operations and recovery

- `/api/health` reports uptime, request latency/errors, worker, feed, exchange, and risk state.
- `/api/admin/audit` exposes the secret-free security and operator audit trail.
- `order_intents` is written before every exchange submission. An `ambiguous`
  intent means the exchange must be queried by its client order ID before retrying.
- Use Emergency Stop for unexpected behavior. Do not clear a stranded-position
  latch until the exchange balance and order history have been reconciled.
- Back up `arbicore.db` while the engine is paused. Restore both the database and
  the same `ARBICORE_MASTER_KEY`; without that key, encrypted credentials are
  intentionally unrecoverable.

## Verification

```powershell
python -m unittest discover -s tests -v
$env:ARBICORE_URL='http://127.0.0.1:5050'; npm.cmd test
```

Run both after every exchange-library upgrade. CCXT remains pinned because a
connector change can alter order behavior.
