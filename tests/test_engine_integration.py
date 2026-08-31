"""The live engine, wired to arbicore: no guessed fill ever reaches an order.

These tests exercise `RealExecutionEngine` itself rather than the library it
now calls. Each one corresponds to a way the original engine could lose real
money and report a profit:

  * reading the requested size as the filled size, then selling coin it had
    never bought
  * approving a book whose top level holds dust and whose real depth is 2%
    away, because it only ever compared `levels[0][0]`
  * subtracting fees additively, which passes trades whose true edge is below
    the floor
  * erasing trades.csv on every restart

The engine is built with `__new__` here, exactly as the older suite does it,
so nothing may depend on attributes that only `__init__` sets.
"""

import contextlib
import csv
import os
import tempfile
import time
import unittest
from pathlib import Path

import arbitrage_bot as bot
from arbicore import orders


class FakeExchange:
    """A ccxt-shaped venue that does only what the test scripts.

    A Mock cannot be used for reconciliation: `fetch_order` has to return a
    payload that differs from the create-order response, which is the whole
    situation being tested.
    """

    has = {"fetchOrder": True, "fetchMyTrades": False, "fetchOrderTrades": False,
           "fetchOpenOrders": False, "fetchClosedOrders": False}

    def __init__(self, books=None, balances=None, buys=None, sells=None,
                 lookups=None, tickers=None):
        self.books = books or {}
        self.balances = balances or {}
        self.buys = [dict(entry) for entry in (buys or [])]
        self.sells = [dict(entry) for entry in (sells or [])]
        self.lookups = dict(lookups or {})
        self.tickers = tickers or {}
        self.buy_requests = []
        self.sell_requests = []
        self.lookup_calls = []

    def fetch_balance(self):
        return self.balances

    def fetch_ticker(self, symbol):
        return self.tickers.get(symbol, {"bid": 0.0, "ask": 0.0})

    def fetch_order_book(self, symbol, limit=None):
        return self.books.get(symbol, {"asks": [], "bids": []})

    def create_market_buy_order(self, symbol, quantity):
        self.buy_requests.append(float(quantity))
        if not self.buys:
            raise AssertionError(f"unexpected buy of {quantity} {symbol}")
        return dict(self.buys.pop(0))

    def create_market_sell_order(self, symbol, quantity):
        self.sell_requests.append(float(quantity))
        if not self.sells:
            raise AssertionError(f"unexpected sell of {quantity} {symbol}")
        return dict(self.sells.pop(0))

    def fetch_order(self, order_id, symbol=None):
        self.lookup_calls.append(order_id)
        payload = self.lookups.get(order_id)
        if payload is None:
            raise RuntimeError(f"order {order_id} is not visible yet")
        if isinstance(payload, list):
            return dict(payload.pop(0)) if payload else {}
        return dict(payload)


def engine_with(clients):
    """An engine built the way the live server builds it, minus the network."""
    engine = bot.RealExecutionEngine.__new__(bot.RealExecutionEngine)
    engine.clients = clients
    engine.markets = {name: {} for name in clients}
    # Reconciliation is real but fast: the point is which number is used, not
    # how long the bot is willing to wait for it.
    engine.poll_timeout = 0.2
    engine.poll_interval = 0.001
    return engine


@contextlib.contextmanager
def real_trading(min_profit=0.15, max_slippage=0.25):
    """Arm the module-level switches the engine reads, then put them back."""
    saved = (bot.REAL_TRADING_ENABLED, bot.EXECUTION_MODE,
             bot.MIN_PROFIT_PCT, bot.MAX_SLIPPAGE_PCT)
    bot.REAL_TRADING_ENABLED = True
    bot.EXECUTION_MODE = "real"
    bot.MIN_PROFIT_PCT = min_profit
    bot.MAX_SLIPPAGE_PCT = max_slippage
    try:
        yield
    finally:
        (bot.REAL_TRADING_ENABLED, bot.EXECUTION_MODE,
         bot.MIN_PROFIT_PCT, bot.MAX_SLIPPAGE_PCT) = saved


BALANCES = {"USDT": {"free": 1000.0}, "BTC": {"free": 1.0}, "ETH": {"free": 1.0}}


class TestOrderBookCheck(unittest.TestCase):

    def book(self, asks=None, bids=None):
        return {"BTC/USDT": {"asks": asks or [], "bids": bids or []}}

    def test_dust_on_top_with_the_real_depth_two_percent_away_is_refused(self):
        # The book shape that made the old check safe on paper: the best price
        # is exactly the quote, but only 0.0001 BTC of it exists.
        client = FakeExchange(books=self.book(
            asks=[[10000, 0.0001], [10200, 100]]))
        engine = engine_with({"binance": client})
        with real_trading():
            with self.assertRaisesRegex(RuntimeError, "moved beyond"):
                engine.check_order_book("binance", "BTC/USDT", "buy", 0.002, 10000)

    def test_the_returned_price_is_the_average_the_size_would_pay(self):
        client = FakeExchange(books=self.book(asks=[[100, 1], [101, 1]]))
        engine = engine_with({"binance": client})
        with real_trading(max_slippage=5):
            average = engine.check_order_book(
                "binance", "BTC/USDT", "buy", 1.5, 100)
        # 1 unit at 100 plus 0.5 at 101 is 150.5 for 1.5 units.
        self.assertAlmostEqual(average, 150.5 / 1.5, places=9)

    def test_a_book_too_shallow_for_the_order_is_refused(self):
        client = FakeExchange(books=self.book(asks=[[100, 0.5]]))
        engine = engine_with({"binance": client})
        with real_trading(max_slippage=5):
            with self.assertRaisesRegex(RuntimeError, "Insufficient buy liquidity"):
                engine.check_order_book("binance", "BTC/USDT", "buy", 2, 100)

    def test_an_empty_book_is_refused_before_any_arithmetic(self):
        engine = engine_with({"binance": FakeExchange(books=self.book())})
        with real_trading():
            with self.assertRaisesRegex(RuntimeError, "No buy liquidity"):
                engine.check_order_book("binance", "BTC/USDT", "buy", 1, 100)

    def test_the_sell_side_is_checked_against_the_bids(self):
        client = FakeExchange(books=self.book(bids=[[9700, 100]]))
        engine = engine_with({"binance": client})
        with real_trading():
            with self.assertRaisesRegex(RuntimeError, "moved beyond"):
                engine.check_order_book("binance", "BTC/USDT", "sell", 0.002, 10000)


def cross_setup(buys, sells, buy_lookups=None, sell_lookups=None,
                asks=None, bids=None):
    """A cheap venue with asks, an expensive one with bids, and the engine."""
    buy_client = FakeExchange(
        books={"BTC/USDT": {"asks": asks or [[10000, 10]], "bids": []}},
        balances=BALANCES, buys=buys, lookups=buy_lookups,
        tickers={"BTC/USDT": {"ask": 10000, "bid": 9999}})
    sell_client = FakeExchange(
        books={"BTC/USDT": {"asks": [], "bids": bids or [[10500, 10]]}},
        balances=BALANCES, sells=sells, lookups=sell_lookups)
    engine = engine_with({"binance": buy_client, "kucoin": sell_client})
    return buy_client, sell_client, engine


class TestCrossExchangeExecution(unittest.TestCase):

    def test_a_missing_filled_field_is_polled_not_read_as_the_requested_size(self):
        # The landmine: the exchange returns `filled: None` on submit and only
        # reports 0.0011 when asked again. The old engine read `amount` -
        # 0.001998 - and sold coin it had never bought.
        buy_client, sell_client, engine = cross_setup(
            buys=[{"id": "b1", "status": "open", "filled": None}],
            buy_lookups={"b1": {"id": "b1", "status": "closed",
                                "filled": 0.0011, "cost": 11.0,
                                "average": 10000}},
            sells=[{"id": "s1", "status": "closed", "filled": 0.0011,
                    "cost": 11.55, "average": 10500}])
        with real_trading():
            result = engine.execute_arbitrage(
                "binance", "kucoin", "BTC/USDT", 20, 10000, 10500)

        self.assertAlmostEqual(buy_client.buy_requests[0], 0.001998, places=9)
        self.assertEqual(len(sell_client.sell_requests), 1)
        self.assertAlmostEqual(sell_client.sell_requests[0], 0.0011, places=9)
        self.assertAlmostEqual(result["filled_quantity"], 0.0011, places=9)
        self.assertAlmostEqual(result["profit_usdt"], 0.55, places=9)
        self.assertTrue(result["buy_fill"]["reconciled"])
        self.assertGreaterEqual(result["buy_fill"]["polls"], 1)

    def test_an_unconfirmable_buy_is_a_recoverable_position_not_a_sell(self):
        buy_client, sell_client, engine = cross_setup(
            buys=[{"id": "b2", "status": "open"}],
            buy_lookups=None,                    # every lookup raises
            sells=[{"id": "s2", "status": "closed", "filled": 1, "cost": 1}])
        with real_trading():
            with self.assertRaises(bot.UnhedgedPositionError) as caught:
                engine.execute_arbitrage(
                    "binance", "kucoin", "BTC/USDT", 20, 10000, 10500)

        error = caught.exception
        self.assertFalse(error.quantity_confirmed)
        self.assertEqual(error.recovery_exchange, "binance")
        self.assertIn("UNCONFIRMED", str(error))
        self.assertIsInstance(error.cause, orders.OrderReconciliationError)
        # Nothing was sold against a size nobody has confirmed.
        self.assertEqual(sell_client.sell_requests, [])
        self.assertGreaterEqual(len(buy_client.lookup_calls), 1)

    def test_a_confirmed_zero_fill_is_not_a_position(self):
        buy_client, sell_client, engine = cross_setup(
            buys=[{"id": "b4", "status": "canceled", "filled": 0}],
            sells=[{"id": "s4", "status": "closed", "filled": 1, "cost": 1}])
        with real_trading():
            with self.assertRaisesRegex(RuntimeError, "no filled quantity"):
                engine.execute_arbitrage(
                    "binance", "kucoin", "BTC/USDT", 20, 10000, 10500)
        self.assertEqual(sell_client.sell_requests, [])
        self.assertNotIsInstance(sell_client, bot.UnhedgedPositionError)

    def test_a_real_partial_sell_is_an_unhedged_position(self):
        _buy, _sell, engine = cross_setup(
            buys=[{"id": "b3", "status": "closed", "filled": 0.002,
                   "cost": 20.0, "average": 10000}],
            sells=[{"id": "s3", "status": "closed", "filled": 0.001,
                    "cost": 10.5, "average": 10500}])
        with real_trading():
            with self.assertRaises(bot.UnhedgedPositionError) as caught:
                engine.execute_arbitrage(
                    "binance", "kucoin", "BTC/USDT", 20, 10000, 10500)

        error = caught.exception
        self.assertAlmostEqual(error.quantity, 0.001, places=9)
        self.assertEqual(error.recovery_exchange, "kucoin")
        self.assertTrue(error.quantity_confirmed)

    def test_step_sized_dust_does_not_halt_the_bot(self):
        # 0.0000005 BTC unsold is not a position anyone can close; halting on
        # it would stop the bot after its first real trade.
        _buy, _sell, engine = cross_setup(
            buys=[{"id": "b5", "status": "closed", "filled": 0.002,
                   "cost": 20.0, "average": 10000}],
            sells=[{"id": "s5", "status": "closed", "filled": 0.0019995,
                    "cost": 20.99, "average": 10500}])
        with real_trading():
            result = engine.execute_arbitrage(
                "binance", "kucoin", "BTC/USDT", 20, 10000, 10500)
        self.assertAlmostEqual(result["unsold_dust"], 0.0000005, places=9)
        self.assertAlmostEqual(result["profit_usdt"], 0.99, places=9)

    def test_quote_currency_fees_are_subtracted_from_the_profit(self):
        # ccxt `cost` excludes the commission, so a trade that looks like +1.00
        # is +0.959 once both USDT fees are paid.
        _buy, _sell, engine = cross_setup(
            buys=[{"id": "b6", "status": "closed", "filled": 0.002,
                   "cost": 20.0, "average": 10000,
                   "fee": {"cost": 0.02, "currency": "USDT"}}],
            sells=[{"id": "s6", "status": "closed", "filled": 0.002,
                    "cost": 21.0, "average": 10500,
                    "fee": {"cost": 0.021, "currency": "USDT"}}])
        with real_trading():
            result = engine.execute_arbitrage(
                "binance", "kucoin", "BTC/USDT", 20, 10000, 10500)
        self.assertAlmostEqual(result["profit_usdt"], 0.959, places=9)


TRI_SYMBOLS = ["BTC/USDT", "ETH/BTC", "ETH/USDT"]
TRI_PRICES = {"BTC/USDT": 10000, "ETH/BTC": 0.05, "ETH/USDT": 530}


def tri_setup(buys, sells, lookups=None):
    """One venue holding a profitable USDT -> BTC -> ETH -> USDT route."""
    client = FakeExchange(
        books={"BTC/USDT": {"asks": [[10000, 10]], "bids": []},
               "ETH/BTC": {"asks": [[0.05, 100]], "bids": []},
               "ETH/USDT": {"asks": [], "bids": [[530, 100]]}},
        balances=BALANCES, buys=buys, sells=sells, lookups=lookups,
        tickers={s: {"ask": TRI_PRICES[s], "bid": TRI_PRICES[s]}
                 for s in TRI_SYMBOLS})
    return client, engine_with({"binance": client})


class TestTriangularExecution(unittest.TestCase):

    def run_route(self, engine, start_usdt=1000):
        return engine.execute_triangular(
            "binance", start_usdt, TRI_PRICES, TRI_SYMBOLS)

    def test_the_middle_leg_is_sized_from_the_confirmed_first_fill(self):
        # The first leg asks for 0.0999 BTC and only gets 0.05. The old engine
        # would then try to buy 1.996 ETH with BTC it did not hold, fail, and
        # leave the BTC stranded; the route now simply runs at the real size.
        client, engine = tri_setup(
            buys=[{"id": "t1", "status": "open", "filled": None},
                  {"id": "t2", "status": "closed", "filled": 0.999,
                   "cost": 0.04995, "average": 0.05}],
            lookups={"t1": {"id": "t1", "status": "closed", "filled": 0.05,
                            "cost": 500.0, "average": 10000}},
            sells=[{"id": "t3", "status": "closed", "filled": 0.999,
                    "cost": 529.47, "average": 530}])
        with real_trading():
            result = self.run_route(engine)

        self.assertAlmostEqual(client.buy_requests[0], 0.0999, places=9)
        self.assertAlmostEqual(client.buy_requests[1], 0.999, places=9)
        self.assertAlmostEqual(client.sell_requests[0], 0.999, places=9)
        self.assertAlmostEqual(result["residual_base"], 0.00005, places=9)
        self.assertAlmostEqual(result["profit_usdt"], 29.47, places=9)
        self.assertEqual(len(result["legs"]), 3)

    def test_a_fee_paid_in_the_received_coin_is_deducted_before_the_next_leg(self):
        # 0.05 BTC filled minus a 0.00005 BTC commission is 0.04995 BTC. Sizing
        # the next leg off 0.05 would ask to spend coin the fee already took.
        client, engine = tri_setup(
            buys=[{"id": "f1", "status": "closed", "filled": 0.05,
                   "cost": 500.0, "average": 10000,
                   "fee": {"cost": 0.00005, "currency": "BTC"}},
                  {"id": "f2", "status": "closed", "filled": 0.998001,
                   "cost": 0.04990005, "average": 0.05}],
            sells=[{"id": "f3", "status": "closed", "filled": 0.998001,
                    "cost": 528.94, "average": 530}])
        with real_trading():
            self.run_route(engine)
        self.assertAlmostEqual(client.buy_requests[1], 0.998001, places=9)

    def test_a_middle_leg_that_never_fills_records_the_btc_for_recovery(self):
        _client, engine = tri_setup(
            buys=[{"id": "m1", "status": "closed", "filled": 0.0999,
                   "cost": 999.0, "average": 10000},
                  {"id": "m2", "status": "canceled", "filled": 0}],
            sells=[])
        with real_trading():
            with self.assertRaises(bot.UnhedgedPositionError) as caught:
                self.run_route(engine)

        error = caught.exception
        self.assertEqual(error.symbol, "BTC/USDT")
        self.assertAlmostEqual(error.quantity, 0.0999, places=9)
        self.assertTrue(error.quantity_confirmed)


class TestProfitGate(unittest.TestCase):
    """The compounded fee maths, at the point where a scan becomes an order."""

    def quotes(self, ask, bid):
        return {"binance": {"ask": ask, "bid": 10000.0},
                "kucoin": {"ask": 99999.0, "bid": bid}}

    def test_a_spread_that_only_clears_the_floor_additively_is_refused(self):
        # Gross 0.35%. Subtracting 2 x 0.1% gives exactly the 0.15% floor, so
        # the old check traded. Compounded it is 0.1492% - a losing trade once
        # slippage is added.
        with real_trading(min_profit=0.15):
            self.assertIsNone(bot.find_opportunity(self.quotes(10000, 10035)))

    def test_a_genuinely_profitable_spread_still_passes(self):
        with real_trading(min_profit=0.15):
            found = bot.find_opportunity(self.quotes(10000, 10040))
        self.assertIsNotNone(found)
        buy_ex, sell_ex, ask, bid, net_pct = found
        self.assertEqual((buy_ex, sell_ex), ("binance", "kucoin"))
        self.assertEqual((ask, bid), (10000, 10040))
        self.assertAlmostEqual(net_pct, 0.1993004, places=7)
        self.assertLess(net_pct, (bid - ask) / ask * 100 - 0.2 + 1e-9)


class BulkVenue:
    """A ccxt-shaped venue that prices every symbol in a single request."""

    has = {"fetchTickers": True}

    def __init__(self, tickers, fail=False):
        self.tickers = tickers
        self.fail = fail
        self.calls = []

    def fetch_tickers(self, symbols=None):
        self.calls.append(symbols)
        if self.fail:
            raise RuntimeError("venue is down")
        return {symbol: dict(payload) for symbol, payload in self.tickers.items()
                if symbols is None or symbol in symbols}


def live_feed_with(clients, symbols):
    """A LiveFeed without the ccxt connection its __init__ performs."""
    instance = bot.LiveFeed.__new__(bot.LiveFeed)
    instance.clients = clients
    instance.markets = {name: {} for name in clients}
    instance.routes = []
    instance.route_candidates = 0
    instance.symbols = list(symbols)
    instance.rejected_quotes = {}
    instance.last_fetch_seconds = 0.0
    return instance


class TestLiveFeedQuotes(unittest.TestCase):
    """The scan is only as good as the prices it runs on."""

    def quote(self, bid, ask, timestamp=None):
        return {"bid": bid, "ask": ask, "timestamp": timestamp}

    def test_each_venue_costs_one_request_no_matter_how_many_symbols(self):
        # 200 symbols used to be 200 requests per venue per scan, which at
        # every exchange's rate limit is minutes, not seconds.
        symbols = [f"C{index}/USDT" for index in range(200)]
        venues = {
            "binance": BulkVenue({s: self.quote(100.0, 100.5) for s in symbols}),
            "kucoin": BulkVenue({s: self.quote(101.0, 101.5) for s in symbols}),
        }
        quotes = live_feed_with(venues, symbols).get_quotes()

        self.assertEqual(len(venues["binance"].calls), 1)
        self.assertEqual(len(venues["kucoin"].calls), 1)
        self.assertEqual(len(quotes), 200)
        self.assertEqual(set(quotes["C7/USDT"]), {"binance", "kucoin"})
        self.assertEqual(quotes["C7/USDT"]["kucoin"]["ask"], 101.5)

    def test_a_stale_price_is_reported_rather_than_scanned(self):
        stale = time.time() * 1000.0 - (bot.MAX_QUOTE_AGE_MS + 60_000)
        venues = {"binance": BulkVenue({
            "BTC/USDT": self.quote(100.0, 100.5),
            "ETH/USDT": self.quote(50.0, 50.2, timestamp=stale)})}
        instance = live_feed_with(venues, ["BTC/USDT", "ETH/USDT"])
        quotes = instance.get_quotes()

        self.assertEqual(list(quotes), ["BTC/USDT"])
        self.assertIn("stale", instance.rejected_quotes["binance"]["ETH/USDT"])

    def test_one_dead_venue_does_not_end_the_scan(self):
        venues = {"binance": BulkVenue({"BTC/USDT": self.quote(100.0, 100.5)}),
                  "kucoin": BulkVenue({}, fail=True)}
        instance = live_feed_with(venues, ["BTC/USDT"])
        quotes = instance.get_quotes()

        self.assertEqual(list(quotes["BTC/USDT"]), ["binance"])
        self.assertIn("kucoin", instance.rejected_quotes)

    def test_the_quote_shape_the_scanner_expects_is_unchanged(self):
        venues = {"binance": BulkVenue({"BTC/USDT": self.quote(100.0, 100.5)})}
        quotes = live_feed_with(venues, ["BTC/USDT"]).get_quotes()
        self.assertEqual(quotes, {"BTC/USDT": {"binance": {"bid": 100.0,
                                                           "ask": 100.5}}})


class TestTradeLogSurvivesRestart(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "trades.csv")
        self.saved = bot.LOG_FILE
        bot.LOG_FILE = self.path
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(setattr, bot, "LOG_FILE", self.saved)

    def rows(self):
        with open(self.path, newline="") as handle:
            return list(csv.reader(handle))

    def test_an_existing_log_is_not_truncated_by_a_restart(self):
        bot.init_csv()
        with open(self.path, "a", newline="") as handle:
            csv.writer(handle).writerow(["2026-01-01T00:00:00", "real"])
        bot.init_csv()          # a second server start
        bot.init_csv()          # and a CLI run alongside it
        rows = self.rows()
        self.assertEqual(rows[0], bot.CSV_HEADER)
        self.assertEqual(rows[-1][:2], ["2026-01-01T00:00:00", "real"])
        self.assertEqual(len(rows), 2)

    def test_a_missing_log_is_created_with_a_header(self):
        self.assertFalse(Path(self.path).exists())
        bot.init_csv()
        self.assertEqual(self.rows(), [bot.CSV_HEADER])


if __name__ == "__main__":
    unittest.main()
