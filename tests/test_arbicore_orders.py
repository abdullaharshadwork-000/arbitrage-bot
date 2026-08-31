"""Order reconciliation: the module that must never guess a fill size.

The bug being guarded against, from the original engine:

    filled_quantity = float(buy_order.get("filled") or buy_order.get("amount") or 0)

On an exchange that returns `filled: None` until the order is fetched again,
that reads the *requested* size as filled and the bot then sells coin it never
bought. Every test here exists to keep that substitution from coming back.
"""

import unittest
from decimal import Decimal

from arbicore.money import ZERO
from arbicore.orders import (DEFAULT_MAX_POLLS, Fill, OrderReconciliationError,
                             find_by_client_id, new_client_order_id,
                             reconcile_order, submit_market_order)


class FakeClient:
    """A ccxt-shaped client whose responses a test can script exactly."""

    def __init__(self, created=None, fetches=None, trades=None, has=None,
                 create_raises=None, open_orders=None, closed_orders=None):
        self.created = created or {}
        self.fetches = list(fetches or [])
        self.trades = list(trades or [])
        self.has = has if has is not None else {
            "fetchOrder": True, "fetchMyTrades": True, "fetchOrderTrades": False,
            "fetchOpenOrders": True, "fetchClosedOrders": True,
        }
        self.create_raises = create_raises
        self.open_orders = list(open_orders or [])
        self.closed_orders = list(closed_orders or [])
        self.fetch_calls = 0
        self.create_calls = []

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        self.create_calls.append((symbol, type_, side, amount, dict(params or {})))
        if self.create_raises:
            raise self.create_raises
        payload = dict(self.created)
        payload.setdefault("clientOrderId", (params or {}).get("clientOrderId"))
        return payload

    def fetch_order(self, order_id, symbol=None):
        self.fetch_calls += 1
        if not self.fetches:
            return {}
        if len(self.fetches) == 1:
            return dict(self.fetches[0])
        return dict(self.fetches.pop(0))

    def fetch_my_trades(self, symbol=None):
        return list(self.trades)

    def fetch_open_orders(self, symbol=None):
        return list(self.open_orders)

    def fetch_closed_orders(self, symbol=None):
        return list(self.closed_orders)


def frozen_clock(start=0.0, step=1.0):
    """A monotonic clock that advances only when asked, and a matching sleep."""
    now = [start]

    def clock():
        return now[0]

    def sleep(seconds):
        now[0] += seconds if seconds else step

    return clock, sleep


class TestReconcileOrder(unittest.TestCase):

    def test_a_settled_order_needs_no_polling(self):
        client = FakeClient()
        fill = reconcile_order(client, "binance", "BTC/USDT", "buy", "0.002",
                               {"id": "1", "status": "closed", "filled": 0.002,
                                "average": 100000.0, "cost": 200.0})
        self.assertTrue(fill.reconciled)
        self.assertEqual(fill.filled_quantity, Decimal("0.002"))
        self.assertEqual(client.fetch_calls, 0)

    def test_filled_none_is_polled_never_replaced_by_requested_size(self):
        clock, sleep = frozen_clock()
        client = FakeClient(fetches=[
            {"id": "1", "status": "open", "filled": None, "amount": 0.002},
            {"id": "1", "status": "closed", "filled": 0.0011, "average": 100000.0},
        ])
        fill = reconcile_order(client, "binance", "BTC/USDT", "buy", "0.002",
                               {"id": "1", "status": "open", "filled": None,
                                "amount": 0.002},
                               sleep=sleep, clock=clock)
        # The exchange said 0.0011. The request said 0.002. The old code would
        # have sold 0.002.
        self.assertEqual(fill.filled_quantity, Decimal("0.0011"))
        self.assertEqual(fill.requested_quantity, Decimal("0.002"))
        self.assertTrue(fill.is_partial)
        self.assertEqual(fill.unfilled_quantity, Decimal("0.0009"))

    def test_a_definite_zero_fill_is_reported_as_zero(self):
        # `filled: 0.0` is an answer, not a missing value, so this settles
        # without polling. A cancel that filled nothing is still "less than
        # requested", so is_partial holds - what must never happen is a
        # caller being handed a non-zero size to sell.
        fill = reconcile_order(FakeClient(), "kucoin", "BTC/USDT", "buy", "0.002",
                               {"id": "1", "status": "canceled", "filled": 0.0})
        self.assertTrue(fill.reconciled)
        self.assertEqual(fill.filled_quantity, ZERO)
        self.assertEqual(fill.unfilled_quantity, Decimal("0.002"))

    def test_quantities_are_derived_from_whichever_pair_is_present(self):
        # cost + average, with filled absent.
        fill = reconcile_order(FakeClient(), "okx", "BTC/USDT", "buy", "0.002",
                               {"id": "1", "status": "closed", "cost": 200.0,
                                "average": 100000.0})
        self.assertEqual(fill.filled_quantity, Decimal("0.002"))
        # filled + cost, with average absent.
        fill = reconcile_order(FakeClient(), "okx", "BTC/USDT", "buy", "0.002",
                               {"id": "1", "status": "closed", "filled": 0.002,
                                "cost": 200.0})
        self.assertEqual(fill.average_price, Decimal("100000"))

    def test_trade_log_is_the_fallback_when_order_lookup_dies(self):
        clock, sleep = frozen_clock()
        client = FakeClient(
            has={"fetchOrder": False, "fetchMyTrades": True,
                 "fetchOrderTrades": False},
            trades=[{"order": "1", "amount": 0.001, "price": 100000.0,
                     "cost": 100.0},
                    {"order": "1", "amount": 0.001, "price": 100200.0,
                     "cost": 100.2},
                    {"order": "2", "amount": 5.0, "price": 1.0, "cost": 5.0}])
        fill = reconcile_order(client, "bybit", "BTC/USDT", "buy", "0.002",
                               {"id": "1", "status": "open"},
                               sleep=sleep, clock=clock)
        self.assertEqual(fill.filled_quantity, Decimal("0.002"))
        self.assertEqual(fill.cost, Decimal("200.2"))     # other order excluded

    def test_an_unconfirmable_fill_raises_with_the_ids_to_investigate(self):
        clock, sleep = frozen_clock()
        client = FakeClient(fetches=[{"id": "1", "status": "open", "filled": None}],
                            trades=[])
        with self.assertRaises(OrderReconciliationError) as caught:
            reconcile_order(client, "binance", "BTC/USDT", "buy", "0.002",
                            {"id": "1", "clientOrderId": "arbi-xyz",
                             "status": "open"},
                            sleep=sleep, clock=clock, poll_timeout=5.0)
        error = caught.exception
        self.assertEqual(error.order_id, "1")
        self.assertEqual(error.client_order_id, "arbi-xyz")
        self.assertIn("OPEN and unknown", str(error))

    def test_a_stalled_clock_cannot_produce_unbounded_api_calls(self):
        # sleep() that does not advance the clock used to spin ~47000 polls,
        # which is an IP ban rather than a reconciliation.
        client = FakeClient(fetches=[{"id": "1", "status": "open", "filled": None}])
        with self.assertRaises(OrderReconciliationError):
            reconcile_order(client, "binance", "BTC/USDT", "buy", "0.002",
                            {"id": "1", "status": "open"},
                            sleep=lambda _s: None, clock=lambda: 0.0)
        self.assertLessEqual(client.fetch_calls, DEFAULT_MAX_POLLS)

    def test_transient_lookup_errors_are_retried_then_reported(self):
        clock, sleep = frozen_clock()

        class Flaky(FakeClient):
            def fetch_order(self, order_id, symbol=None):
                self.fetch_calls += 1
                raise TimeoutError("gateway timeout")

        client = Flaky(trades=[])
        with self.assertRaises(OrderReconciliationError) as caught:
            reconcile_order(client, "binance", "BTC/USDT", "buy", "0.002",
                            {"id": "1", "status": "open"},
                            sleep=sleep, clock=clock, poll_timeout=3.0)
        self.assertIn("gateway timeout", str(caught.exception))
        self.assertGreater(client.fetch_calls, 1)

    def test_fees_are_read_from_either_shape(self):
        single = reconcile_order(FakeClient(), "binance", "BTC/USDT", "buy",
                                 "0.002",
                                 {"id": "1", "status": "closed", "filled": 0.002,
                                  "average": 100000.0,
                                  "fee": {"cost": 0.2, "currency": "USDT"}})
        self.assertEqual(single.fee_cost, Decimal("0.2"))
        self.assertEqual(single.fee_currency, "USDT")
        listed = reconcile_order(FakeClient(), "binance", "BTC/USDT", "buy",
                                 "0.002",
                                 {"id": "1", "status": "closed", "filled": 0.002,
                                  "average": 100000.0,
                                  "fees": [{"cost": 0.1, "currency": "BNB"},
                                           {"cost": 0.05, "currency": "BNB"}]})
        self.assertEqual(listed.fee_cost, Decimal("0.15"))
        self.assertEqual(listed.fee_currency, "BNB")


class TestSubmitMarketOrder(unittest.TestCase):

    def test_every_order_carries_a_client_id_we_generated(self):
        client = FakeClient(created={"id": "1", "status": "closed",
                                     "filled": 0.002, "average": 100000.0})
        submit_market_order(client, "binance", "BTC/USDT", "buy", "0.002")
        params = client.create_calls[0][4]
        self.assertTrue(params["clientOrderId"].startswith("arbi"))

    def test_ids_are_unique(self):
        self.assertNotEqual(new_client_order_id(), new_client_order_id())

    def test_a_failed_submit_searches_for_its_own_order_before_giving_up(self):
        # The dangerous case: the request reached the exchange, the response did
        # not come back. Without the clientOrderId lookup this is an orphan.
        client = FakeClient(create_raises=ConnectionError("connection reset"))
        recovered = {"id": "77", "status": "closed", "filled": 0.002,
                     "average": 100000.0}

        def fetch_open(symbol=None):
            found = dict(recovered)
            found["clientOrderId"] = client.create_calls[0][4]["clientOrderId"]
            return [found]

        client.fetch_open_orders = fetch_open
        fill = submit_market_order(client, "binance", "BTC/USDT", "buy", "0.002")
        self.assertEqual(fill.order_id, "77")
        self.assertEqual(fill.filled_quantity, Decimal("0.002"))

    def test_a_failed_submit_with_no_trace_raises_rather_than_assuming(self):
        client = FakeClient(create_raises=ConnectionError("connection reset"))
        with self.assertRaises(OrderReconciliationError) as caught:
            submit_market_order(client, "binance", "BTC/USDT", "buy", "0.002")
        self.assertIn("verify manually", str(caught.exception))

    def test_find_by_client_id_checks_open_then_closed(self):
        client = FakeClient(closed_orders=[{"clientOrderId": "arbi-1", "id": "9"}])
        self.assertEqual(find_by_client_id(client, "BTC/USDT", "arbi-1")["id"], "9")
        self.assertIsNone(find_by_client_id(client, "BTC/USDT", "arbi-missing"))


class TestFill(unittest.TestCase):

    def test_as_dict_is_json_ready(self):
        fill = Fill("binance", "BTC/USDT", "buy", "1", "c1", "closed",
                    Decimal("0.002"), Decimal("0.001"), Decimal("100000"),
                    Decimal("100"))
        payload = fill.as_dict()
        self.assertIsInstance(payload["filled_quantity"], float)
        self.assertEqual(payload["status"], "closed")

    def test_unfilled_never_goes_negative(self):
        over = Fill("binance", "BTC/USDT", "buy", "1", "c1", "closed",
                    Decimal("1"), Decimal("2"), Decimal("1"), Decimal("2"))
        self.assertEqual(over.unfilled_quantity, ZERO)
        self.assertFalse(over.is_partial)


if __name__ == "__main__":
    unittest.main(verbosity=2)
