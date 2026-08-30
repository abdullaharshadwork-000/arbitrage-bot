# ArbiCore — Route-Aware Arbitrage Engine

A Python bot that watches supported exchanges, discovers cross-exchange and
single-exchange triangular opportunities, and can run in paper or explicitly enabled
real execution mode with balance, fee, slippage, and recovery safeguards.

Built for beginners. Everything you might want to change sits in one clearly marked
CONFIG section at the top of the file.

---

## 1. What the bot actually does

Every few seconds it:

1. Fetches the current **bid** (highest buyer) and **ask** (lowest seller) price for each
   coin pair (BTC/USDT, ETH/USDT, …) on each exchange.
2. Finds the exchange with the **lowest ask** and the one with the **highest bid**.
3. Checks whether the gap is bigger than the trading fees (you pay a fee on **both**
   sides — buying and selling — roughly 0.1% + 0.1% = 0.2%).
4. If the gap survives the fees, it executes a **paper trade**: buy on the cheap exchange,
   sell on the expensive one, and records the virtual profit.
5. Saves every trade to `trades.csv` and prints a summary as it runs.

## 2. Realistic expectations (read this first)

The viral post that inspired this claimed $68 → $750,000. That is not how arbitrage works:

- Real arbitrage gaps are **tiny** — usually 0.05% to 0.5%.
- A $200 trade at 0.3% profit earns **about $0.60**. Watch the demo: the bot prints
  exactly these numbers.
- Gaps close in seconds because professional firms with servers _inside_ the exchanges
  compete for them.
- This project is excellent for **learning** how bots, APIs, and arbitrage work.
  Treat any later real-money attempt as an experiment you can afford to lose, not income.

## 3. Setup (about 5 minutes)

### Step 1 — Install Python

- **Windows/Mac:** download from [python.org](https://www.python.org/downloads/)
  (on Windows, tick **"Add Python to PATH"** during installation).
- **Linux:** `sudo apt install python3 python3-pip`

Check it worked — open a terminal (Command Prompt on Windows) and type:

```bash
python3 --version
```

(On Windows you may need `python` instead of `python3` — use that everywhere below.)

### Step 2 — Install the exchange library

```bash
pip install ccxt
```

(On Windows: `pip install ccxt` or `python -m pip install ccxt`)

### Step 3 — Run the demo (works even without internet)

Open a terminal **inside the folder** where you saved `arbitrage_bot.py` and run:

```bash
python3 arbitrage_bot.py
```

You'll see opportunities appear and virtual profits being logged. Press **Ctrl+C** to stop.
The demo uses fake prices, so gaps appear on purpose much more often than in real markets —
this is just so you can watch the mechanics.

### Step 4 — Switch to real live prices (still paper trading!)

Open `arbitrage_bot.py` in any text editor (Notepad works) and change one line:

```python
MODE = "live"
```

Run it again. The bot now pulls **real, live prices** from Binance, KuCoin, OKX and Bybit.
You don't need an account or API keys — price data is public. Trades are still simulated.

> **What you'll notice in live mode:** profitable gaps are rare and small, and most scans
> print "no profitable gap (fees eat small gaps)". That's the real market — the demo
> exaggerates gaps so you can learn the mechanics.

## 4. Settings you can change (top of the file)

| Setting           | What it does                                     | Default                          |
| ----------------- | ------------------------------------------------ | -------------------------------- |
| `MODE`            | `"demo"` (fake prices) or `"live"` (real prices) | `"demo"`                         |
| `EXCHANGES`       | Which supported exchange(s) to watch             | binance, kucoin, okx, bybit      |
| `SYMBOLS`         | Which coin pairs to scan                         | BTC, ETH, SOL, XRP, DOGE vs USDT |
| `TRADE_SIZE_USDT` | Virtual money per trade                          | 200                              |
| `TAKER_FEE`       | Fee per trade (0.001 = 0.1%)                     | 0.001                            |
| `MIN_PROFIT_PCT`  | Only trade if profit after fees ≥ this %         | 0.15                             |
| `CHECK_INTERVAL`  | Seconds between scans                            | 5                                |

## 5. Understanding the output

```
[14:32:10] OPPORTUNITY ETH/USDT  buy kucoin @ 3,499.86 -> sell bybit @ 3,518.35 | net +0.33% = $+0.65
```

- **buy kucoin @ 3,499.86** — the cheapest place to buy ETH right now
- **sell bybit @ 3,518.35** — the most expensive place to sell it right now
- **net +0.33%** — gap after paying both fees
- **= $+0.65** — virtual profit on a $200 trade

Every trade is also appended to `trades.csv`, which you can open in Excel.

## 6. What it would take to trade for real (later, carefully)

The jump from paper to real is bigger than people expect:

1. **Funded accounts on BOTH exchanges** — you can't wait for a transfer mid-trade;
   moving crypto between exchanges takes minutes, and the gap lasts seconds. Real arb
   traders hold both USDT _and_ the coin on every exchange and rebalance occasionally.
2. **Withdrawal/transfer fees** when rebalancing — these eat thin margins.
3. **API keys** with trading permission added to the bot (**never** enable withdrawal
   permission on bot API keys).
4. **Slippage** — the price you see is for the top of the order book; a $5,000 order
   may fill at a worse average price than a $200 one.
5. **Speed** — this Python bot checks every few seconds; professional competitors react
   in milliseconds. You will lose the best gaps to them.
6. **Small start** — if you ever try real money, use an amount you're fully comfortable
   losing, and run the paper version for weeks first.

## 7. Ideas to extend it

- Add more symbols or exchanges (ccxt supports 100+)
- Alert yourself (Telegram/Discord message) when a big gap appears
- Use real **order book depth** instead of top bid/ask (more realistic profit math)
- Track how long each gap lasts to understand the competition
- Add a rebalance simulator (moving funds between exchanges with realistic delays/fees)

---

**Files in this folder**

| File               | Purpose                                             |
| ------------------ | --------------------------------------------------- |
| `arbitrage_bot.py` | The bot itself                                      |
| `trades.csv`       | Log of simulated trades (created when the bot runs) |
| `README.md`        | This guide                                          |
