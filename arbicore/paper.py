"""Persistent paper account using the same shared asset balances for all pairs."""
import copy
from .ledger import Ledger
from .money import D, ZERO
from .simulator import FillSimulator, synthetic_book


class PaperAccount:
    def __init__(self, exchanges, symbols, capital, prices, strategy, snapshot=None):
        self.symbols = list(symbols)
        self.initial = float(capital)
        self.profit = 0.0
        self.recovery = None
        self.signal_state = {}
        self.signal_cache = {}
        self.marks = {"USDT": 1.0}
        self.ledger = Ledger()
        self.mark(prices)
        if snapshot:
            self.ledger = Ledger(snapshot["balances"])
            self.initial = float(snapshot["initial"])
            self.profit = float(snapshot.get("profit", 0))
            self.recovery = snapshot.get("recovery")
            self.signal_state = snapshot.get("signal_state", {})
            self.marks.update(snapshot.get("marks", {}))
            self.symbols = list(dict.fromkeys(self.symbols + snapshot.get("symbols", [])))
            self.mark(prices)
        else:
            share = D(capital) / len(exchanges)
            assets = [s.split("/")[0] for s in symbols if s.endswith("/USDT")]
            assets = list(dict.fromkeys(assets))
            for exchange in exchanges:
                invested = share / 2 if strategy == "cross_exchange" and assets else ZERO
                self.ledger.credit(exchange, "USDT", share - invested)
                for asset in assets:
                    self.ledger.credit(exchange, asset,
                                       invested / len(assets) / D(self.marks[asset]))

    def mark(self, prices):
        # Convert quote currencies too (e.g. ETH/BTC), never treat BTC as USDT.
        for _ in range(3):
            for symbol, price in (prices or {}).items():
                base, quote = symbol.split("/")
                value = D(price)
                if value.is_finite() and value > ZERO and quote in self.marks:
                    self.marks[base] = float(value * D(self.marks[quote]))

    def total_value(self, prices):
        self.mark(prices)
        return float(self.ledger.value_in_quote(self.marks))

    @property
    def usdt(self):
        # Compatibility view: cash appears once per venue, never multiplied by pairs.
        return {ex: {s: float(self.ledger.get(ex, "USDT")) if i == 0 else 0.0
                     for i, s in enumerate(self.symbols)} for ex in self.ledger.exchanges()}

    @property
    def coin(self):
        return {ex: {s: float(self.ledger.get(ex, s.split("/")[0]))
                     for s in self.symbols} for ex in self.ledger.exchanges()}

    def snapshot(self):
        balances = {ex: {asset: str(self.ledger.get(ex, asset))
                         for asset in self.ledger.currencies(ex)}
                    for ex in self.ledger.exchanges()}
        return copy.deepcopy({"version": 1, "initial": self.initial, "profit": self.profit,
                "balances": balances, "marks": self.marks, "symbols": self.symbols,
                "recovery": self.recovery, "signal_state": self.signal_state})

    def restore(self, snapshot):
        self.ledger = Ledger(snapshot["balances"])
        self.initial = snapshot["initial"]
        self.profit = snapshot["profit"]
        self.marks = dict(snapshot["marks"])
        self.symbols = list(snapshot["symbols"])
        self.recovery = copy.deepcopy(snapshot.get("recovery"))
        self.signal_state = copy.deepcopy(snapshot.get("signal_state", {}))
        self.signal_cache = {}

    def execute(self, cfg, candidate, quote_feed):
        if self.recovery:
            raise RuntimeError("Paper account has an unresolved route; recovery is required before further trades.")
        clients = getattr(quote_feed, "clients", {})
        prices = candidate["cycle"]["prices"] if candidate.get("cycle") else {
            candidate["symbol"]: candidate["ask"]}

        def book(exchange, symbol, side):
            if cfg["mode"] == "live":
                return clients[exchange].fetch_order_book(symbol, limit=50).get(side, [])
            reference = (candidate["bid"] if side == "bids" and not candidate.get("cycle")
                         else prices[symbol])
            return synthetic_book(reference, side,
                                  top_size=D(cfg["trade_size"]) / D(reference) / 4)

        def step(exchange, symbol):
            client = clients.get(exchange)
            if client is None:
                return D("0.00000001")
            market = client.market(symbol)
            precision = (market.get("precision") or {}).get("amount")
            if precision is None:
                raise RuntimeError(f"Missing amount precision for {exchange} {symbol}")
            # CCXT's precisionMode, not a numeric guess, defines the units.
            if client.precisionMode == 4:  # TICK_SIZE
                return D(precision)
            if client.precisionMode == 2:  # DECIMAL_PLACES
                return D(1).scaleb(-int(precision))
            raise RuntimeError("Unsupported paper amount precision mode")

        simulator = FillSimulator(self.ledger, book, lambda ex, sym: cfg["fee"],
                                  step, latency_ms=250)
        if cfg["strategy"] == "triangular":
            symbols = candidate["cycle"]["symbols"]
            result = simulator.execute_triangular(
                candidate["buy_exchange"], list(zip(symbols, ["buy", "buy", "sell"])),
                cfg["trade_size"], prices, cfg["max_slippage"], cfg["min_profit"])
        else:
            result = simulator.execute_cross_exchange(
                candidate["buy_exchange"], candidate["sell_exchange"], candidate["symbol"],
                cfg["trade_size"], candidate["ask"], candidate["bid"],
                cfg["max_slippage"], cfg["min_profit"])
        if result.stranded:
            self.recovery = result.as_dict()
            raise RuntimeError(f"Paper route requires recovery: {result.stranded_quantity} "
                               f"{result.stranded_currency} on {result.stranded_exchange}")
        if not result.ok:
            return None, {"reason": result.reason}
        self.profit = float(D(self.profit) + result.realized_profit)
        return float(D(cfg["trade_size"]) + result.realized_profit), result.as_dict()
