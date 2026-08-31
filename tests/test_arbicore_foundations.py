"""Decimal arithmetic, order-book walking, and shared balances."""

import unittest
from decimal import Decimal

from arbicore import books
from arbicore.ledger import InsufficientBalance, Ledger, split_symbol
from arbicore.money import (D, ONE, ZERO, after_fee, floor_to_step,
                            net_spread_pct, pct_change, round_money,
                            step_from_precision, to_float)


class TestMoney(unittest.TestCase):

    def test_float_conversion_avoids_binary_artifacts(self):
        # The whole reason this module exists.
        self.assertEqual(D(0.1), Decimal("0.1"))
        self.assertNotEqual(Decimal(0.1), Decimal("0.1"))

    def test_missing_values_collapse_to_default(self):
        for value in (None, "", "not a number", object()):
            self.assertEqual(D(value), ZERO)
        self.assertIsNone(D(None, default=None))
        self.assertIsNone(D("garbage", default=None))

    def test_step_from_precision_handles_both_ccxt_conventions(self):
        self.assertEqual(step_from_precision(8), Decimal("1E-8"))    # Binance
        self.assertEqual(step_from_precision(0.001), Decimal("0.001"))  # Bitfinex
        self.assertIsNone(step_from_precision(None))
        self.assertIsNone(step_from_precision(0))

    def test_floor_to_step_never_rounds_up(self):
        # Rounding up would exceed the balance we just checked.
        self.assertEqual(floor_to_step("0.999999999", "0.001"), Decimal("0.999"))
        self.assertEqual(floor_to_step("1.9", ONE), ONE)
        self.assertEqual(floor_to_step("5", None), Decimal("5"))

    def test_net_spread_compounds_fees_rather_than_summing(self):
        compounded = net_spread_pct("100", "101", "0.001", "0.001")
        additive = Decimal("1") - Decimal("0.2")   # the old gross - 2*fee*100
        # 1.01 * 0.999 * 0.999 - 1 = 0.798101%, not the 0.8% the shortcut gives.
        self.assertAlmostEqual(float(compounded), 0.798101, places=6)
        self.assertLess(compounded, additive)

    def test_net_spread_of_a_zero_price_is_zero_not_an_exception(self):
        self.assertEqual(net_spread_pct(0, 100, "0.001", "0.001"), ZERO)

    def test_helpers(self):
        self.assertEqual(after_fee("100", "0.001"), Decimal("99.9"))
        self.assertEqual(pct_change("100", "101"), Decimal("1"))
        self.assertEqual(pct_change(0, 101), ZERO)
        self.assertEqual(round_money("1.123456789"), Decimal("1.12345679"))
        self.assertIsInstance(to_float("1.5"), float)


class TestBooks(unittest.TestCase):

    # The book shape that defeats a top-of-book check: dust on top, real size
    # 2% away.
    THIN = [[100.0, 0.01], [102.0, 10.0]]
    DEEP = [[100.0, 5.0], [100.1, 5.0], [100.2, 5.0]]

    def test_vwap_reflects_the_whole_walk_not_the_best_price(self):
        estimate = books.fill_for_quantity(self.THIN, "1")
        self.assertEqual(estimate.best_price, Decimal("100"))
        self.assertAlmostEqual(float(estimate.average_price), 101.98, places=2)
        self.assertGreater(estimate.slippage_pct, Decimal("1.9"))

    def test_partial_fill_is_flagged_not_silently_accepted(self):
        estimate = books.fill_for_quantity(self.DEEP, "100")
        self.assertFalse(estimate.complete)
        self.assertEqual(estimate.quantity, Decimal("15"))

    def test_notional_walk_never_overspends_the_budget(self):
        estimate = books.fill_for_notional(self.DEEP, "700")
        self.assertLessEqual(estimate.notional, Decimal("700"))
        self.assertTrue(estimate.complete)

    def test_empty_and_malformed_books_are_survivable(self):
        for levels in ([], None, [["x", "y"]], [[0, 5]], [[100]]):
            self.assertEqual(books.fill_for_quantity(levels, "1").quantity, ZERO)
            self.assertEqual(books.fill_for_notional(levels, "100").quantity, ZERO)

    def test_book_order_is_never_resorted(self):
        # A corrupted book must look corrupted, not be quietly repaired.
        backwards = [[102.0, 1.0], [100.0, 1.0]]
        self.assertEqual(books.fill_for_quantity(backwards, "1").average_price,
                         Decimal("102"))

    def test_rejection_uses_average_price_against_the_reference(self):
        thin = books.fill_for_quantity(self.THIN, "1")
        self.assertIsNotNone(books.rejection_reason("buy", thin, "100", "0.25"))
        self.assertIsNone(books.rejection_reason("buy", thin, "100", "5.0"))

    def test_rejection_reports_drift_from_the_quote_not_from_the_book(self):
        # A book uniformly 1% worse than the quote has zero internal slippage
        # and is still 1% of loss; the message must say 1%.
        flat = books.fill_for_quantity([[99.0, 10.0]], "1")
        reason = books.rejection_reason("sell", flat, "100", "0.25")
        self.assertIn("1.000%", reason)

    def test_rejection_explains_each_distinct_refusal(self):
        self.assertIn("no buy liquidity",
                      books.rejection_reason("buy", books.EMPTY_FILL, "100", "1"))
        partial = books.fill_for_quantity(self.DEEP, "100")
        self.assertIn("covers only",
                      books.rejection_reason("buy", partial, "100", "1"))
        self.assertIn("reference price is missing",
                      books.rejection_reason("buy", partial, 0, "1",
                                             require_complete=False))

    def test_visible_notional(self):
        self.assertEqual(books.visible_notional([[100.0, 2.0], [200.0, 1.0]]),
                         Decimal("400"))


class TestLedger(unittest.TestCase):

    def setUp(self):
        self.ledger = Ledger.funded(["binance", "kucoin"],
                                    {"USDT": 1000, "BTC": 1})

    def test_split_symbol_defaults_the_quote(self):
        self.assertEqual(split_symbol("ETH/BTC"), ("ETH", "BTC"))
        self.assertEqual(split_symbol("ETH"), ("ETH", "USDT"))

    def test_balances_are_shared_across_symbols(self):
        # The old PaperWallet gave every symbol its own cash/2 per exchange.
        self.ledger.debit("binance", "USDT", 600)
        self.assertEqual(self.ledger.get("binance", "USDT"), Decimal("400"))

    def test_debit_beyond_balance_raises_before_mutating(self):
        with self.assertRaises(InsufficientBalance) as caught:
            self.ledger.debit("binance", "USDT", 5000)
        self.assertEqual(self.ledger.get("binance", "USDT"), Decimal("1000"))
        self.assertEqual(caught.exception.available, Decimal("1000"))

    def test_negative_amounts_are_rejected_on_both_sides(self):
        with self.assertRaises(ValueError):
            self.ledger.credit("binance", "USDT", -1)
        with self.assertRaises(ValueError):
            self.ledger.debit("binance", "USDT", -1)

    def test_apply_fill_moves_both_currencies(self):
        self.ledger.apply_fill("binance", "BTC/USDT", "buy", "0.002", "200")
        self.assertEqual(self.ledger.get("binance", "USDT"), Decimal("800"))
        self.assertEqual(self.ledger.get("binance", "BTC"), Decimal("1.002"))

    def test_fees_are_charged_in_the_currency_the_exchange_billed(self):
        # BNB/KCS fee discounts bill in the native token; assuming quote
        # denomination overstates profit.
        self.ledger.set_balance("binance", "BNB", 1)
        self.ledger.apply_fill("binance", "BTC/USDT", "buy", "0.002", "200",
                               fee_cost="0.01", fee_currency="BNB")
        self.assertEqual(self.ledger.get("binance", "BNB"), Decimal("0.99"))
        self.assertEqual(self.ledger.get("binance", "BTC"), Decimal("1.002"))

    def test_unknown_side_is_an_error(self):
        with self.assertRaises(ValueError):
            self.ledger.apply_fill("binance", "BTC/USDT", "hold", "1", "1")

    def test_snapshot_omits_zero_balances(self):
        self.ledger.set_balance("binance", "DOGE", 0)
        self.assertNotIn("DOGE", self.ledger.snapshot("binance"))

    def test_unpriced_currency_is_skipped_not_valued_at_zero(self):
        self.ledger.set_balance("binance", "MYSTERY", 100)
        total = self.ledger.value_in_quote({"BTC": 100000})
        # 2000 USDT + 2 BTC, with MYSTERY simply absent.
        self.assertEqual(total, Decimal("202000"))

    def test_inventory_skew_reports_the_two_extremes(self):
        self.ledger.set_balance("binance", "USDT", 0)     # all coin
        self.ledger.set_balance("kucoin", "BTC", 0)       # all quote
        skew = self.ledger.inventory_skew("BTC/USDT", {"BTC": 100000})
        self.assertEqual(skew["binance"], 1.0)
        self.assertEqual(skew["kucoin"], 0.0)

    def test_skew_of_an_empty_venue_is_zero_not_a_division_error(self):
        empty = Ledger()
        self.assertEqual(empty.inventory_skew("BTC/USDT", {"BTC": 1}), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
