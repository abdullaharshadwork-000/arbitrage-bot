#!/usr/bin/env python3
"""Safe launcher for live trading configuration.

Loads the local live_config.py, refuses to continue unless the real-trading
configuration validates, then hands over to server.serve() — the same code path
as `python server.py`, so the port fallback and the session-token banner apply
here too.
"""

import sys

import arbitrage_bot


def main():
    arbitrage_bot.load_live_config_if_present()

    if arbitrage_bot.MODE != "live":
        arbitrage_bot.MODE = "live"

    validation = arbitrage_bot.validate_real_trading_config()
    if not validation["ok"]:
        print(f"[!] Live trading is not ready: {validation['message']}")
        print("Fill in live_config.py with real keys and set REAL_TRADING_ENABLED = True.")
        sys.exit(1)

    import server

    print("=" * 64)
    print("  LIVE TRADING STARTUP CHECK PASSED")
    print(f"  Trade size: ${arbitrage_bot.TRADE_SIZE_USDT:.2f} USDT")
    print(f"  Min profit: {arbitrage_bot.MIN_PROFIT_PCT:.2f}%")
    print(f"  Exchanges: {', '.join(arbitrage_bot.EXCHANGES)}")
    print("=" * 64)

    server.bootstrap()
    server.serve()


if __name__ == "__main__":
    main()
