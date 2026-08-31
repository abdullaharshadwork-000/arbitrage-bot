"""Paper fills that are allowed to lose: the simulator's rejections are the point.

Each test here corresponds to a trade the original paper path booked as a win:
top-of-book fill, unlimited depth, zero latency, per-symbol wallets that never
ran dry. The assertions are mostly about *refusals* - the trades that should
never have counted.
"""

import unittest
from decimal import Decimal

from arbicore.ledger import Ledger
from arbicore.money import ZERO
from arbicore.simulator import (FillSimulator, SimulatedLeg, SimulatedTrade,
                                chain_route, synthetic_book)

# A book with real size at the quote, used when the test is not about depth.
DEEP_ASKS = [[100.0, 50.0], [100.01, 50.0], [100.02, 50.0]]
DEEP_BIDS = [[101.0, 50.0], [100.99, 50.0], [100.98, 50.0]]
# Dust on top, real size 2% away: the shape a best-price check waves through.
THIN_ASKS = [[100.0, 0.01], [102.0, 100.0]]


def books_from(mapping):
    """book_source built from {(exchange, symbol, side): levels}."""
    def source(exchange, symbol, side):
        return mapping.get((exchange, symbol, side), [])
    return source


def simulator(mapping, ledger=None, fee="0.001", latency_ms=0, step=None):
    return FillSimulator(
        ledger=ledger if ledger is not None else Ledger.funded(
            ["A", "B", "X"], {"USDT": 10000, "BTC": 100}),
        book_source=books_from(mapping),
        fee_provider=lambda exchange, symbol: Decimal(fee),
        step_provider=lambda exchange, symbol: step,
        latency_ms=latency_ms,
        sleep=lambda _seconds: None)


class TestCrossExchange(unittest.TestCase):

    def base_books(self):
        return {("A", "BTC/USDT", "asks"): DEEP_ASKS,
                ("B", "BTC/USDT", "bids"): DEEP_BIDS}

    def test_a_real_edge_fills_and_realized_tracks_expected(self):
        sim = simulator(self.base_books())
        trade = sim.execute_cross_exchange("A", "B", "BTC/USDT", "200",
                                           "100", "101")
        self.assertTrue(trade.ok, trade.reason)
        self.assertEqual(len(trade.legs), 2)
        # Deep books either side of the quote: slippage is a rounding artefact,
        # not a cost.
        self.assertLess(abs(float(trade.slippage_cost)), 0.01)
        self.assertGreater(trade.realized_profit, ZERO)

    def test_a_thin_top_level_is_refused_not_filled_at_the_best_price(self):
        mapping = self.base_books()
        mapping[("A", "BTC/USDT", "asks")] = THIN_ASKS
        trade = simulator(mapping).execute_cross_exchange(
            "A", "B", "BTC/USDT", "200", "100", "101")
        self.assertFalse(trade.ok)
        self.assertIn("buy leg rejected", trade.reason)
        self.assertIn("above the quote", trade.reason)

    def test_a_book_that_moved_during_the_latency_wait_is_refused(self):
        # book_source is consulted after the wait, so a source that reports a
        # worse market on the second call models exactly that.
        state = {"calls": 0}

        def source(exchange, symbol, side):
            if side == "asks":
                return DEEP_ASKS
            state["calls"] += 1
            return [[97.0, 50.0]]          # the bid collapsed 4% while we waited

        sim = FillSimulator(ledger=Ledger.funded(["A", "B"], {"USDT": 10000,
                                                             "BTC": 100}),
                            book_source=source, latency_ms=250,
                            fee_provider=lambda e, s: Decimal("0.001"),
                            sleep=lambda _s: None)
        trade = sim.execute_cross_exchange("A", "B", "BTC/USDT", "200",
                                           "100", "101")
        self.assertFalse(trade.ok)
        self.assertIn("sell leg rejected", trade.reason)
        self.assertEqual(state["calls"], 1)

    def test_a_drained_venue_says_which_currency_ran_out(self):
        ledger = Ledger.funded(["A", "B"], {"USDT": 10000, "BTC": 100})
        ledger.set_balance("A", "USDT", 5)
        trade = simulator(self.base_books(), ledger=ledger).execute_cross_exchange(
            "A", "B", "BTC/USDT", "200", "100", "101")
        self.assertFalse(trade.ok)
        self.assertIn("A is out of USDT", trade.reason)
        self.assertIn("rebalance required", trade.reason)
        # Nothing was touched: a refusal must not move balances.
        self.assertEqual(ledger.get("A", "USDT"), Decimal("5"))

    def test_a_venue_with_no_coin_to_sell_is_caught_before_the_sell_book(self):
        ledger = Ledger.funded(["A", "B"], {"USDT": 10000, "BTC": 100})
        ledger.set_balance("B", "BTC", 0)
        trade = simulator(self.base_books(), ledger=ledger).execute_cross_exchange(
            "A", "B", "BTC/USDT", "200", "100", "101")
        self.assertFalse(trade.ok)
        self.assertIn("B is out of BTC", trade.reason)

    def test_an_edge_that_collapses_at_fill_prices_is_vetoed(self):
        # Both books are individually inside the slippage tolerance, but the
        # real prices leave no profit. The old engine committed on scan prices.
        mapping = {("A", "BTC/USDT", "asks"): [[100.2, 50.0]],
                   ("B", "BTC/USDT", "bids"): [[100.25, 50.0]]}
        trade = simulator(mapping).execute_cross_exchange(
            "A", "B", "BTC/USDT", "200", "100", "100.5")
        self.assertFalse(trade.ok)
        self.assertIn("edge collapsed", trade.reason)

    def test_partial_depth_fills_less_and_reports_the_shortfall(self):
        mapping = {("A", "BTC/USDT", "asks"): [[100.0, 0.3]],   # 30 USDT of 200
                   ("B", "BTC/USDT", "bids"): DEEP_BIDS}
        trade = simulator(mapping).execute_cross_exchange(
            "A", "B", "BTC/USDT", "200", "100", "101")
        self.assertTrue(trade.ok, trade.reason)
        buy_leg = trade.legs[0]
        self.assertFalse(buy_leg.complete)
        self.assertEqual(buy_leg.filled_quantity, Decimal("0.3"))
        # A third of the size earns roughly a third of the profit, so the
        # expected figure - computed on the full notional - overstates.
        self.assertLess(trade.realized_profit, trade.expected_profit)

    def test_an_empty_book_is_a_refusal_not_a_crash(self):
        trade = simulator({}).execute_cross_exchange(
            "A", "B", "BTC/USDT", "200", "100", "101")
        self.assertFalse(trade.ok)
        self.assertIn("no buy liquidity", trade.reason)

    def test_step_size_floors_the_size_and_never_rounds_up(self):
        sim = simulator(self.base_books(), step=Decimal("0.001"))
        trade = sim.execute_cross_exchange("A", "B", "BTC/USDT", "200",
                                           "100", "101")
        self.assertTrue(trade.ok, trade.reason)
        quantized = trade.legs[0].filled_quantity
        self.assertEqual(quantized, quantized.quantize(Decimal("0.001")))
        self.assertLessEqual(quantized, Decimal("2"))

    def test_a_step_size_coarser_than_the_trade_refuses_rather_than_rounding_up(self):
        sim = simulator(self.base_books(), step=Decimal("10"))
        trade = sim.execute_cross_exchange("A", "B", "BTC/USDT", "200",
                                           "100", "101")
        self.assertFalse(trade.ok)
        self.assertIn("rounds to zero", trade.reason)

    def test_the_ledger_shows_the_inventory_drift_the_strategy_creates(self):
        ledger = Ledger.funded(["A", "B"], {"USDT": 10000, "BTC": 100})
        sim = simulator(self.base_books(), ledger=ledger)
        for _ in range(3):
            self.assertTrue(sim.execute_cross_exchange(
                "A", "B", "BTC/USDT", "200", "100", "101").ok)
        # A spent quote and gained coin; B did the reverse. This is the drift
        # per-symbol wallets could never show.
        self.assertLess(ledger.get("A", "USDT"), Decimal("10000"))
        self.assertGreater(ledger.get("A", "BTC"), Decimal("100"))
        self.assertGreater(ledger.get("B", "USDT"), Decimal("10000"))
        self.assertLess(ledger.get("B", "BTC"), Decimal("100"))


class TestChainRoute(unittest.TestCase):

    def test_a_closed_loop_expands_to_spent_and_received(self):
        steps = chain_route([("BTC/USDT", "buy"), ("ETH/BTC", "buy"),
                             ("ETH/USDT", "sell")], "USDT")
        self.assertEqual([(s[0], s[2], s[3]) for s in steps],
                         [("BTC/USDT", "USDT", "BTC"),
                          ("ETH/BTC", "BTC", "ETH"),
                          ("ETH/USDT", "ETH", "USDT")])

    def test_a_route_that_does_not_come_home_is_not_an_arbitrage(self):
        with self.assertRaises(ValueError) as caught:
            chain_route([("BTC/USDT", "buy")], "USDT")
        self.assertIn("does not close", str(caught.exception))

    def test_a_broken_link_names_the_leg_that_breaks_it(self):
        with self.assertRaises(ValueError) as caught:
            chain_route([("BTC/USDT", "buy"), ("SOL/USDT", "sell")], "USDT")
        self.assertIn("route breaks at SOL/USDT", str(caught.exception))

    def test_empty_routes_and_bad_sides_are_rejected(self):
        with self.assertRaises(ValueError):
            chain_route([], "USDT")
        with self.assertRaises(ValueError):
            chain_route([("BTC/USDT", "hold")], "USDT")


class TestTriangular(unittest.TestCase):

    ROUTE = [("BTC/USDT", "buy"), ("ETH/BTC", "buy"), ("ETH/USDT", "sell")]
    # 100000 USDT/BTC, 0.04 BTC/ETH -> 4000 USDT/ETH implied; selling at 4100
    # is the edge.
    QUOTES = {"BTC/USDT": "100000", "ETH/BTC": "0.04", "ETH/USDT": "4100"}

    def deep_books(self):
        return {
            ("X", "BTC/USDT", "asks"): [[100000.0, 10.0]],
            ("X", "ETH/BTC", "asks"): [[0.04, 500.0]],
            ("X", "ETH/USDT", "bids"): [[4100.0, 500.0]],
        }

    def test_a_profitable_loop_closes_and_records_every_leg(self):
        sim = simulator(self.deep_books())
        trade = sim.execute_triangular("X", self.ROUTE, "200", self.QUOTES)
        self.assertTrue(trade.ok, trade.reason)
        self.assertEqual(trade.strategy, "triangular")
        self.assertEqual(len(trade.legs), 3)
        self.assertGreater(trade.realized_profit, ZERO)
        self.assertFalse(trade.stranded)

    def test_a_flat_market_is_vetoed_before_any_capital_moves(self):
        ledger = Ledger.funded(["X"], {"USDT": 10000})
        flat = dict(self.QUOTES, **{"ETH/USDT": "4000"})
        trade = simulator(self.deep_books(), ledger=ledger).execute_triangular(
            "X", self.ROUTE, "200", flat)
        self.assertFalse(trade.ok)
        self.assertEqual(len(trade.legs), 0)
        self.assertIn("below the", trade.reason)
        self.assertEqual(ledger.get("X", "USDT"), Decimal("10000"))

    def test_an_unpriced_leg_stops_the_route(self):
        trade = simulator(self.deep_books()).execute_triangular(
            "X", self.ROUTE, "200", {"BTC/USDT": "100000", "ETH/BTC": "0.04"})
        self.assertFalse(trade.ok)
        self.assertIn("no quoted price", trade.reason)

    def test_a_dead_final_leg_strands_a_real_position(self):
        # The case with no clean exit: two legs filled, the third has no book,
        # and the capital is sitting in ETH on a venue that cannot sell it.
        mapping = self.deep_books()
        mapping[("X", "ETH/USDT", "bids")] = []
        ledger = Ledger.funded(["X"], {"USDT": 10000})
        trade = simulator(mapping, ledger=ledger).execute_triangular(
            "X", self.ROUTE, "200", self.QUOTES)
        self.assertFalse(trade.ok)
        self.assertEqual(len(trade.legs), 2)
        self.assertTrue(trade.stranded)
        self.assertEqual(trade.stranded_currency, "ETH")
        self.assertEqual(trade.stranded_exchange, "X")
        self.assertGreater(trade.stranded_quantity, ZERO)
        # The USDT really is gone; this is not a hypothetical.
        self.assertLess(ledger.get("X", "USDT"), Decimal("10000"))
        self.assertGreater(ledger.get("X", "ETH"), ZERO)

    def test_a_dead_first_leg_is_a_clean_veto_with_nothing_stranded(self):
        mapping = self.deep_books()
        mapping[("X", "BTC/USDT", "asks")] = []
        trade = simulator(mapping).execute_triangular(
            "X", self.ROUTE, "200", self.QUOTES)
        self.assertFalse(trade.ok)
        self.assertFalse(trade.stranded)
        self.assertEqual(len(trade.legs), 0)

    def test_latency_is_charged_on_every_hop_not_just_the_first(self):
        waits = []
        sim = FillSimulator(
            ledger=Ledger.funded(["X"], {"USDT": 10000}),
            book_source=books_from(self.deep_books()),
            fee_provider=lambda e, s: Decimal("0.001"),
            latency_ms=250, sleep=waits.append)
        trade = sim.execute_triangular("X", self.ROUTE, "200", self.QUOTES)
        self.assertTrue(trade.ok, trade.reason)
        self.assertEqual(waits, [0.25, 0.25, 0.25])

    def test_realized_falls_short_of_the_quoted_promise_when_depth_is_thin(self):
        mapping = self.deep_books()
        mapping[("X", "ETH/BTC", "asks")] = [[0.04, 0.01], [0.0401, 500.0]]
        sim = simulator(mapping)
        trade = sim.execute_triangular("X", self.ROUTE, "200", self.QUOTES,
                                       max_slippage_pct="1.0")
        self.assertTrue(trade.ok, trade.reason)
        self.assertGreater(trade.slippage_cost, ZERO)

    def test_a_zero_notional_is_refused(self):
        trade = simulator(self.deep_books()).execute_triangular(
            "X", self.ROUTE, "0", self.QUOTES)
        self.assertFalse(trade.ok)
        self.assertIn("notional is zero", trade.reason)

    def test_a_drained_start_currency_is_refused_before_leg_one(self):
        ledger = Ledger.funded(["X"], {"USDT": 5})
        trade = simulator(self.deep_books(), ledger=ledger).execute_triangular(
            "X", self.ROUTE, "200", self.QUOTES)
        self.assertFalse(trade.ok)
        self.assertIn("X is out of USDT", trade.reason)

    def test_quoted_route_return_is_the_number_the_old_engine_trusted(self):
        sim = simulator(self.deep_books())
        steps = chain_route(self.ROUTE, "USDT")
        promised = sim.quoted_route_return("X", steps, "200", self.QUOTES)
        self.assertGreater(promised, Decimal("200"))
        self.assertIsNone(sim.quoted_route_return("X", steps, "200",
                                                  {"BTC/USDT": "100000"}))


class TestSyntheticBook(unittest.TestCase):

    def test_asks_climb_and_bids_fall_from_the_mid(self):
        asks = synthetic_book("100", "asks", depth_levels=3, top_size="1")
        bids = synthetic_book("100", "bids", depth_levels=3, top_size="1")
        self.assertEqual([price for price, _size in asks],
                         [Decimal("100"), Decimal("100.02"), Decimal("100.04")])
        self.assertEqual([price for price, _size in bids],
                         [Decimal("100"), Decimal("99.98"), Decimal("99.96")])

    def test_size_grows_as_the_price_worsens(self):
        levels = synthetic_book("100", "asks", depth_levels=3, top_size="1",
                                size_growth="2")
        self.assertEqual([size for _price, size in levels],
                         [Decimal("1"), Decimal("2"), Decimal("4")])

    def test_a_nonsense_mid_produces_no_book_rather_than_negative_prices(self):
        self.assertEqual(synthetic_book(0, "asks"), [])
        self.assertEqual(synthetic_book("-5", "asks"), [])

    def test_bids_stop_before_crossing_zero(self):
        levels = synthetic_book("1", "bids", depth_levels=99, step_pct="50")
        self.assertTrue(all(price > ZERO for price, _size in levels))


class TestSimulatedTrade(unittest.TestCase):

    def test_as_dict_is_json_ready_including_the_stranded_fields(self):
        leg = SimulatedLeg("A", "BTC/USDT", "buy", Decimal("100"), Decimal("2"),
                           Decimal("2"), Decimal("100.1"), Decimal("200.2"),
                           Decimal("0.2"), "BTC", True, 250)
        trade = SimulatedTrade(ok=True, legs=(leg,), symbol="BTC/USDT",
                               expected_profit=Decimal("1"),
                               realized_profit=Decimal("0.4"))
        payload = trade.as_dict()
        self.assertAlmostEqual(payload["slippage_cost"], 0.6, places=9)
        self.assertFalse(payload["stranded"])
        self.assertIsInstance(payload["legs"][0]["slippage_pct"], float)

    def test_slippage_sign_is_a_cost_on_both_sides(self):
        buy = SimulatedLeg("A", "S", "buy", Decimal("100"), ZERO, ZERO,
                           Decimal("101"), ZERO, ZERO, "", True, 0)
        sell = SimulatedLeg("A", "S", "sell", Decimal("100"), ZERO, ZERO,
                            Decimal("99"), ZERO, ZERO, "", True, 0)
        self.assertEqual(buy.slippage_pct, Decimal("1"))
        self.assertEqual(sell.slippage_pct, Decimal("1"))

    def test_an_unpriced_leg_reports_zero_slippage_not_a_division_error(self):
        leg = SimulatedLeg("A", "S", "buy", ZERO, ZERO, ZERO, ZERO, ZERO, ZERO,
                           "", False, 0)
        self.assertEqual(leg.slippage_pct, ZERO)


if __name__ == "__main__":
    unittest.main(verbosity=2)
