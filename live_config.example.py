# Copy this file to live_config.py and fill in your real credentials.
# Never commit your secret API keys to version control.

REAL_TRADING_ENABLED = False
MODE = "live"
EXECUTION_MODE = "paper"  # change to "real" only after paper validation
TRADING_STRATEGY = "cross_exchange"  # use "triangular" with one exchange
# For one-exchange triangular trading, set EXCHANGES to one supported exchange.
# Example: EXCHANGES = ["kucoin"]

EXCHANGE_CREDENTIALS = {
    "binance": {
        "apiKey": "PASTE_YOUR_BINANCE_API_KEY",
        "secret": "PASTE_YOUR_BINANCE_SECRET",
    },
    "kucoin": {
        "apiKey": "PASTE_YOUR_KUCOIN_API_KEY",
        "secret": "PASTE_YOUR_KUCOIN_SECRET",
    },
    "okx": {
        "apiKey": "PASTE_YOUR_OKX_API_KEY",
        "secret": "PASTE_YOUR_OKX_SECRET",
    },
    "bybit": {
        "apiKey": "PASTE_YOUR_BYBIT_API_KEY",
        "secret": "PASTE_YOUR_BYBIT_SECRET",
    },
}

# Risk settings for live trading - keep these tiny at first.
TRADE_SIZE_USDT = 20.0
TAKER_FEE = 0.001
MIN_PROFIT_PCT = 0.35
MAX_SLIPPAGE_PCT = 0.25
CHECK_INTERVAL = 2

# Safety: only enable once you have verified everything in test mode.
# This script is intentionally not auto-triggered in production.
