"""Optional WebSocket order-book cache with fail-safe REST fallback.

The cache is exchange-neutral. ``CcxtProStream`` is loaded only when streaming
is explicitly enabled, so the default install and every unit test remain fully
offline. Consumers must treat an incomplete/stale snapshot as unavailable and
fall back to the normal REST feed.
"""

import asyncio
import importlib
import threading
import time
from dataclasses import dataclass


@dataclass
class BookState:
    bid: float
    ask: float
    timestamp: float
    sequence: int = 0


class StreamingBookCache:
    def __init__(self, max_age_seconds=3.0, clock=time.time):
        self.max_age_seconds = float(max_age_seconds)
        self._clock = clock
        self._books = {}
        self._lock = threading.Lock()
        self.sequence_gaps = 0
        self.reconnects = 0
        self.last_error = ""

    def update(self, exchange, symbol, bid, ask, sequence=0, timestamp=None):
        bid, ask = float(bid or 0), float(ask or 0)
        if bid <= 0 or ask <= 0 or ask < bid:
            return False
        key = (str(exchange), str(symbol))
        sequence = int(sequence or 0)
        with self._lock:
            previous = self._books.get(key)
            if previous and sequence and previous.sequence and sequence <= previous.sequence:
                return False
            if previous and sequence and previous.sequence and sequence > previous.sequence + 1:
                self.sequence_gaps += 1
                self._books.pop(key, None)
                return False
            self._books[key] = BookState(
                bid, ask, float(timestamp or self._clock()), sequence)
        return True

    def snapshot(self, exchanges, symbols):
        now = self._clock()
        result = {symbol: {} for symbol in symbols}
        with self._lock:
            for symbol in symbols:
                for exchange in exchanges:
                    book = self._books.get((exchange, symbol))
                    if not book:
                        continue
                    age = now - book.timestamp
                    if age < 0 or age > self.max_age_seconds:
                        continue
                    result[symbol][exchange] = {
                        "bid": book.bid, "ask": book.ask,
                        "age_ms": age * 1000.0, "source": "websocket",
                    }
        return {symbol: venues for symbol, venues in result.items() if venues}

    def complete(self, exchanges, symbols):
        snapshot = self.snapshot(exchanges, symbols)
        return all(all(exchange in snapshot.get(symbol, {}) for exchange in exchanges)
                   for symbol in symbols)

    def health(self):
        with self._lock:
            return {"books": len(self._books), "sequence_gaps": self.sequence_gaps,
                    "reconnects": self.reconnects, "last_error": self.last_error}


class CcxtProStream:
    """Background public-book watcher. Failures never bypass REST safety."""

    def __init__(self, exchanges, symbols, sandbox=False, cache=None):
        self.exchanges = list(exchanges)
        self.symbols = list(symbols)
        self.sandbox = bool(sandbox)
        self.cache = cache or StreamingBookCache()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return True
        # Validate availability before starting a thread whose import error
        # would otherwise be invisible to the operator.
        importlib.import_module("ccxt.pro")
        self._thread = threading.Thread(target=self._thread_main, daemon=True,
                                        name="arbicore-market-stream")
        self._thread.start()
        return True

    def stop(self, timeout=5.0):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def _thread_main(self):
        try:
            asyncio.run(self._run())
        except Exception as exc:
            self.cache.last_error = f"stream stopped: {type(exc).__name__}: {exc}"

    async def _run(self):
        ccxtpro = importlib.import_module("ccxt.pro")
        clients = {}
        try:
            for exchange in self.exchanges:
                client = getattr(ccxtpro, exchange)({"enableRateLimit": True})
                if self.sandbox:
                    client.set_sandbox_mode(True)
                clients[exchange] = client
            tasks = [asyncio.create_task(self._watch(client, exchange, symbol))
                     for exchange, client in clients.items() for symbol in self.symbols]
            await asyncio.gather(*tasks)
        finally:
            await asyncio.gather(*(client.close() for client in clients.values()),
                                 return_exceptions=True)

    async def _watch(self, client, exchange, symbol):
        delay = 0.25
        while not self._stop.is_set():
            try:
                book = await client.watch_order_book(symbol)
                bids, asks = book.get("bids") or [], book.get("asks") or []
                if bids and asks:
                    stamp = book.get("timestamp")
                    self.cache.update(
                        exchange, symbol, bids[0][0], asks[0][0],
                        sequence=book.get("nonce") or 0,
                        timestamp=(float(stamp) / 1000.0 if stamp else time.time()),
                    )
                delay = 0.25
            except Exception as exc:
                self.cache.reconnects += 1
                self.cache.last_error = f"{exchange} {symbol}: {type(exc).__name__}: {exc}"
                await asyncio.sleep(delay)
                delay = min(delay * 2, 15.0)
