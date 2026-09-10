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
from collections import OrderedDict
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


class PrivateOrderStream:
    """Authenticated order updates with an in-memory waitable cache.

    Exchange events are the primary acknowledgement path; callers still use
    REST reconciliation when the stream is unavailable or an event is missed.
    The callback must be idempotent because reconnects may replay events.
    """

    def __init__(self, exchanges, credential_map, sandbox=False, on_event=None):
        self.exchanges = list(exchanges)
        self.credential_map = {
            name: dict((credential_map or {}).get(name) or {})
            for name in self.exchanges
        }
        self.sandbox = bool(sandbox)
        self.on_event = on_event
        self._stop = threading.Event()
        self._thread = None
        self._started = threading.Event()
        self._loop = None
        self._tasks = []
        # A daemon may run for weeks.  Keep enough recent acknowledgements for
        # REST fallbacks without retaining every order for the process lifetime.
        self._events = OrderedDict()
        self._max_events = 2000
        self._conditions = {}
        self._lock = threading.Lock()
        self.running = False
        self.reconnects = 0
        self.last_event_at = None
        self.last_error = ""

    def start(self):
        if self._thread and self._thread.is_alive():
            return True
        importlib.import_module("ccxt.pro")
        self._stop.clear()
        self._started.clear()
        self._thread = threading.Thread(
            target=self._thread_main, daemon=True, name="arbicore-private-orders")
        self._thread.start()
        self._started.wait(5.0)
        return self.running

    def stop(self, timeout=5.0):
        self._stop.set()
        loop = self._loop
        tasks = list(self._tasks)
        if loop and loop.is_running() and tasks:
            def cancel_watchers():
                for task in tasks:
                    task.cancel()
            loop.call_soon_threadsafe(cancel_watchers)
        if self._thread:
            self._thread.join(timeout)
        stopped = not (self._thread and self._thread.is_alive())
        if not stopped:
            self.last_error = "private order stream did not stop within the timeout"
        return stopped

    def health(self):
        return {"running": self.running, "reconnects": self.reconnects,
                "last_event_at": self.last_event_at,
                "last_error": self.last_error}

    def publish(self, exchange, payload):
        if not isinstance(payload, dict):
            return False
        client_id = str(
            payload.get("clientOrderId")
            or (payload.get("info") or {}).get("clientOrderId") or "")
        if not client_id:
            return False
        normalized = dict(payload)
        normalized["exchange"] = exchange
        with self._lock:
            self._events[client_id] = normalized
            self._events.move_to_end(client_id)
            while len(self._events) > self._max_events:
                self._events.popitem(last=False)
            condition = self._conditions.get(client_id)
            if condition:
                condition.notify_all()
        self.last_event_at = time.time()
        if self.on_event:
            self.on_event(exchange, normalized)
        return True

    def wait_for(self, client_order_id, timeout=2.0):
        if not client_order_id:
            return None
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._lock:
            condition = self._conditions.setdefault(
                client_order_id, threading.Condition(self._lock))
            while client_order_id not in self._events:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                condition.wait(remaining)
            result = self._events.get(client_order_id)
            self._conditions.pop(client_order_id, None)
            return result

    def _thread_main(self):
        try:
            asyncio.run(self._run())
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.last_error = f"stream stopped: {type(exc).__name__}: {exc}"
        finally:
            self.running = False
            self._started.set()

    async def _run(self):
        ccxtpro = importlib.import_module("ccxt.pro")
        clients = {}
        self._loop = asyncio.get_running_loop()
        try:
            for exchange in self.exchanges:
                credentials = self.credential_map.get(exchange) or {}
                config = {"enableRateLimit": True, **credentials}
                if exchange == "binance":
                    config["options"] = {"fetchCurrencies": False,
                                         "adjustForTimeDifference": True,
                                         "recvWindow": 5000}
                client = getattr(ccxtpro, exchange)(config)
                if self.sandbox:
                    client.set_sandbox_mode(True)
                clients[exchange] = client
            self.running = True
            self._started.set()
            self._tasks = [
                asyncio.create_task(self._watch_orders(client, exchange))
                for exchange, client in clients.items()
            ]
            await asyncio.gather(*self._tasks)
        finally:
            for task in self._tasks:
                if not task.done():
                    task.cancel()
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            await asyncio.gather(*(client.close() for client in clients.values()),
                                 return_exceptions=True)
            self._tasks = []
            self._loop = None

    async def _watch_orders(self, client, exchange):
        delay = 0.25
        while not self._stop.is_set():
            try:
                updates = await client.watch_orders()
                if isinstance(updates, dict):
                    updates = [updates]
                for update in updates or []:
                    self.publish(exchange, update)
                delay = 0.25
            except Exception as exc:
                self.reconnects += 1
                self.last_error = f"{exchange}: {type(exc).__name__}: {exc}"
                await asyncio.sleep(delay)
                delay = min(delay * 2, 15.0)
