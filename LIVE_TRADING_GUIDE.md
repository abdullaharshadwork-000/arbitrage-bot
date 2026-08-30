# Live trading guide

This project is now intentionally locked down so that real-money trading is not enabled by accident.

## 1. Required setup

1. Copy `live_config.example.py` to `live_config.py`.
2. Fill in your exchange API keys.
3. Set `REAL_TRADING_ENABLED = True` only after verifying your credentials and balances.
4. Keep `MODE = "live"`.

## 2. Safety requirements

- Never enable live trading on a production account without testing first.
- Use tiny trade sizes first.
- Keep your exchanges funded and avoid using more capital than you can afford to lose.
- Use realistic slippage and latency assumptions before scaling up.
- Never share API keys in code repositories or chat windows.

## 3. Suggested startup sequence

1. Run the bot in demo mode.
2. Validate the scan logic, fee math, and paths.
3. Confirm live price feeds work with public data.
4. Test account balance fetches with a small sandbox or tiny real balance.
5. Start with a tiny trade size, for example 10-20 USDT.
6. Monitor the bot for latency and failed fills.

## 4. Risk warning

Cross-exchange arbitrage is not a stable profit machine. Real spreads are often small and disappear quickly. Execution risk, fees, balance mismatches, and failed order fills can erase expected gains.

Use this bot only as an educational and carefully controlled execution experiment.
