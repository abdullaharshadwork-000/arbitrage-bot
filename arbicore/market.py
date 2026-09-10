"""Credential-free market overview. This service cannot submit exchange orders.

Public candles are shared by venue/pair, never account data. Entry signals are
computed from completed one-minute candles using the engine's own analyser.
"""
import copy
import math
import threading
import time

from .signals import analyse


TIMEFRAMES = {"1m": 60, "5m": 300, "15m": 900}


def public_client(exchange):
    import ccxt
    return getattr(ccxt, exchange)({"enableRateLimit": True, "timeout": 5000,
        "maxRetriesOnFailure": 0,
        "options": {"defaultType": "spot", "fetchCurrencies": False,
                    "fetchMarkets": {"types": ["spot"], "fetchTickersFees": False},
                    "maxRetriesOnFailure": 0}})


def candle_rows(raw, timeframe, now):
    step = TIMEFRAMES[timeframe]
    result, previous = [], None
    if not isinstance(raw, list) or not raw or len(raw) > 500:
        raise ValueError("Candle history is empty or invalid")
    for row in raw:
        if not isinstance(row, (list, tuple)) or len(row) < 6 or any(isinstance(x, bool) for x in row[:6]):
            raise ValueError("Malformed candle")
        stamp, opening, high, low, close, volume = map(float, row[:6])
        if (not all(math.isfinite(v) for v in (stamp, opening, high, low, close, volume))
                or stamp < 0 or stamp % (step * 1000) or stamp / 1000 > now + 1
                or min(opening, high, low, close) <= 0 or volume < 0
                or not low <= min(opening, close) <= max(opening, close) <= high):
            raise ValueError("Invalid OHLCV values")
        stamp = int(stamp / 1000)
        if previous is not None and stamp - previous != step:
            raise ValueError("Candle sequence has gaps or duplicate timestamps")
        result.append({"time": stamp, "open": opening, "high": high, "low": low,
                       "close": close, "volume": volume, "closed": stamp + step <= now})
        previous = stamp
    return result


def ema_points(rows, period):
    """Warm up once; incomplete chart candles are allowed, not entry decisions."""
    result, average = [], None
    for index, row in enumerate(rows):
        if index < period - 1:
            continue
        average = (sum(r["close"] for r in rows[:period]) / period if average is None
                   else average + 2 / (period + 1) * (row["close"] - average))
        result.append({"time": row["time"], "value": average})
    return result


def market_payload(raw, minute_raw, timeframe, now):
    rows = candle_rows(raw, timeframe, now)
    minute_rows = candle_rows(minute_raw, "1m", now)
    last_closed = next((row for row in reversed(minute_rows) if row["closed"]), None)
    chart_closed = next((row for row in reversed(rows) if row["closed"]), None)
    fresh = (last_closed is not None and 0 <= now - (last_closed["time"] + 60) <= 60
             and chart_closed is not None
             and 0 <= now - (chart_closed["time"] + TIMEFRAMES[timeframe]) <= TIMEFRAMES[timeframe])
    if fresh:
        signal = analyse(minute_raw, now * 1000)
    else:
        signal = {"action": "wait", "reason": "Live candle feed is stale or not warmed up"}
    if signal.get("ema9") is None:
        regime = "Unavailable" if not fresh else "Warming up"
    else:
        fast, slow = signal["ema9"], signal["ema21"]
        regime = ("Rising" if fast > slow and signal["momentum5_pct"] > 0
                  else "Falling" if fast < slow and signal["momentum5_pct"] < 0 else "Mixed / sideways")
    return {"candles": rows, "ema9": ema_points(rows, 9), "ema21": ema_points(rows, 21),
            "signal": signal, "regime": regime, "fresh": fresh,
            "last_price": rows[-1]["close"], "as_of": now, "source": "public_exchange_candles",
            "decision_timeframe": "1m", "chart_timeframe": timeframe,
            "last_closed_candle": last_closed["time"] if last_closed else None,
            "execution_allowed": False,
            "execution_note": "Analysis only. Engine risk, account and protection checks are required before any order."}


class MarketOverview:
    def __init__(self, exchanges, symbols, factory=public_client, clock=time.time, ttl=5):
        self.exchanges, self.symbols = frozenset(exchanges), frozenset(symbols)
        self.factory, self.clock, self.ttl = factory, clock, ttl
        self._clients, self._cache = {}, {}
        self._locks = {exchange: threading.Lock() for exchange in self.exchanges}

    def snapshot(self, exchange, symbol, timeframe="1m"):
        if exchange not in self.exchanges or symbol not in self.symbols or timeframe not in TIMEFRAMES:
            raise ValueError("Unsupported exchange, symbol or chart timeframe")
        key = (exchange, symbol, timeframe)
        lock = self._locks[exchange]
        if not lock.acquire(timeout=0.2):
            return self._unavailable(exchange, symbol, timeframe, "Market data request already in progress")
        try:
            cached = self._cache.get(key)
            now = self.clock()
            if cached and 0 <= now - cached[0] < self.ttl:
                result = copy.deepcopy(cached[1])
                if result.get("fresh"):
                    chart_closed = next((row for row in reversed(result["candles"]) if row["closed"]), None)
                    if (now > result["last_closed_candle"] + 120 or chart_closed is None
                            or now > chart_closed["time"] + 2 * TIMEFRAMES[timeframe]):
                        result.update(fresh=False, regime="Unavailable")
                        result["signal"] = {"action": "wait", "reason": "Cached candle feed is stale; no new entry signal"}
                return result
            try:
                if exchange not in self._clients:
                    self._clients[exchange] = self.factory(exchange)
                client = self._clients[exchange]
                started = time.monotonic()
                raw = client.fetch_ohlcv(symbol, timeframe, limit=180)
                minute_raw = raw if timeframe == "1m" else client.fetch_ohlcv(symbol, "1m", limit=80)
                if time.monotonic() - started > 12:
                    raise ValueError("Market data response exceeded freshness budget")
                result = market_payload(raw, minute_raw, timeframe, self.clock())
                result.update(exchange=exchange, symbol=symbol, available=True, error=None)
            except Exception as exc:
                # No raw exchange messages, signed URLs, fabricated candles, or
                # fallback to an unrelated exchange after a venue failure.
                result = self._unavailable(exchange, symbol, timeframe, type(exc).__name__)
            self._cache[key] = (self.clock(), result)
            return copy.deepcopy(result)
        finally:
            lock.release()

    def _unavailable(self, exchange, symbol, timeframe, error):
        return {"available": False, "fresh": False, "exchange": exchange, "symbol": symbol,
                "chart_timeframe": timeframe, "decision_timeframe": "1m", "as_of": self.clock(),
                "regime": "Unavailable", "candles": [], "ema9": [], "ema21": [],
                "signal": {"action": "wait", "reason": "Current exchange data unavailable; no new entry signal"},
                "execution_allowed": False, "execution_note": "Market feed unavailable", "error": error}
