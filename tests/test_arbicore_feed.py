"""Quote collection: bulk requests, stale prices, and route ranking.

Every test here stands for a way the serial per-symbol feed lost money or
lost time:

  * pricing 2,794 symbols one request at a time, so each scan ran on quotes
    from several scans ago
  * accepting a ticker the exchange stamped minutes earlier, whose spread had
    already closed
  * accepting a one-sided or crossed ticker, which reads as free profit
  * scanning thousands of arithmetically-valid routes whose legs have no
    volume, crowding out the handful that can actually be filled
"""

import unittest

from arbicore import feed


NOW = 1_700_000_000_000.0     # a fixed "now" in ms, so nothing depends on the clock


def ticker(bid, ask, timestamp=NOW, **extra):
    payload = {"bid": bid, "ask": ask, "timestamp": timestamp}
    payload.update(extra)
    return payload


class TestMergeTickerBatch(unittest.TestCase):

    def merge(self, payload, symbols=None, max_age_ms=10_000):
        return feed.merge_ticker_batch(payload, symbols, now_ms=NOW,
                                       max_age_ms=max_age_ms)

    def test_a_fresh_two_sided_ticker_is_kept(self):
        quotes, rejected = self.merge({"BTC/USDT": ticker(100.0, 100.5)})
        self.assertEqual(quotes["BTC/USDT"]["bid"], 100.0)
        self.assertEqual(quotes["BTC/USDT"]["ask"], 100.5)
        self.assertEqual(rejected, {})

    def test_a_stale_ticker_is_dropped_with_its_age(self):
        quotes, rejected = self.merge(
            {"BTC/USDT": ticker(100.0, 100.5, timestamp=NOW - 30_000)})
        self.assertEqual(quotes, {})
        self.assertIn("stale by 30.0s", rejected["BTC/USDT"])

    def test_a_ticker_just_inside_the_window_is_still_tradeable(self):
        quotes, _ = self.merge(
            {"BTC/USDT": ticker(100.0, 100.5, timestamp=NOW - 9_999)})
        self.assertIn("BTC/USDT", quotes)

    def test_a_missing_timestamp_is_not_treated_as_stale(self):
        # Several venues omit it. Rejecting those would silently reduce the
        # scan to the venues that happen to report one.
        quotes, rejected = self.merge({"BTC/USDT": ticker(100.0, 100.5, timestamp=None)})
        self.assertIn("BTC/USDT", quotes)
        self.assertEqual(rejected, {})

    def test_a_one_sided_book_is_refused(self):
        quotes, rejected = self.merge({
            "BTC/USDT": ticker(None, 100.5),
            "ETH/USDT": ticker(100.0, None),
            "SOL/USDT": ticker(0.0, 100.5),
        })
        self.assertEqual(quotes, {})
        self.assertEqual(set(rejected), {"BTC/USDT", "ETH/USDT", "SOL/USDT"})

    def test_a_crossed_quote_is_refused_rather_than_read_as_free_profit(self):
        # ask below bid means the two sides came from different snapshots. Read
        # literally it is an instant risk-free profit on a single venue.
        quotes, rejected = self.merge({"BTC/USDT": ticker(101.0, 100.0)})
        self.assertEqual(quotes, {})
        self.assertEqual(rejected["BTC/USDT"], "crossed")

    def test_symbols_outside_the_request_are_ignored(self):
        quotes, rejected = self.merge(
            {"BTC/USDT": ticker(100.0, 100.5), "JUNK/USDT": ticker(1.0, 2.0)},
            symbols=["BTC/USDT"])
        self.assertEqual(list(quotes), ["BTC/USDT"])
        self.assertEqual(rejected, {})

    def test_a_malformed_entry_does_not_abort_the_batch(self):
        quotes, rejected = self.merge(
            {"BTC/USDT": ticker(100.0, 100.5), "ETH/USDT": None})
        self.assertEqual(list(quotes), ["BTC/USDT"])
        self.assertEqual(rejected["ETH/USDT"], "malformed")

    def test_a_zero_max_age_disables_the_staleness_check(self):
        quotes, _ = self.merge(
            {"BTC/USDT": ticker(100.0, 100.5, timestamp=NOW - 999_999)},
            max_age_ms=0)
        self.assertIn("BTC/USDT", quotes)


class TestQuoteAge(unittest.TestCase):

    def test_a_small_forward_skew_counts_as_fresh(self):
        self.assertEqual(feed.quote_age_ms(NOW + 5_000, NOW), 0.0)

    def test_a_large_forward_skew_makes_the_age_unusable(self):
        # Our clock disagrees with theirs by more than a minute. Judging
        # freshness would then discard every quote on the venue.
        self.assertIsNone(feed.quote_age_ms(NOW + 300_000, NOW))

    def test_a_normal_age_is_the_difference(self):
        self.assertEqual(feed.quote_age_ms(NOW - 1_500, NOW), 1_500)

    def test_junk_timestamps_are_unusable_not_zero(self):
        for value in (None, 0, -1, "", "yesterday"):
            self.assertIsNone(feed.quote_age_ms(value, NOW), value)


def route(*assets):
    """A route through the given assets, e.g. route("BTC", "ETH")."""
    first, second = assets
    return {"route": ["USDT", first, second, "USDT"],
            "symbols": [f"{first}/USDT", f"{second}/{first}", f"{second}/USDT"]}


class TestRouteRanking(unittest.TestCase):

    def test_routes_are_ranked_by_their_thinnest_leg(self):
        # The BTC->ETH route has a deep first leg but a 50 USDT middle leg; the
        # BTC->BNB route is uniformly 5,000. The thin middle leg is the one
        # that decides whether either can be filled.
        tickers = {
            "BTC/USDT": {"quoteVolume": 900_000_000},
            "ETH/BTC": {"quoteVolume": 50},
            "ETH/USDT": {"quoteVolume": 400_000_000},
            "BNB/BTC": {"quoteVolume": 5_000},
            "BNB/USDT": {"quoteVolume": 5_000},
        }
        ranked = feed.rank_routes([route("BTC", "ETH"), route("BTC", "BNB")], tickers)
        self.assertEqual([r["route"][2] for r in ranked], ["BNB", "ETH"])

    def test_the_limit_is_a_hard_cap(self):
        routes = [route("BTC", f"C{index}") for index in range(200)]
        self.assertEqual(len(feed.rank_routes(routes, {}, limit=60)), 60)

    def test_routes_with_no_volume_data_keep_their_discovery_order(self):
        routes = [route("BTC", "ETH"), route("BTC", "BNB"), route("BTC", "SOL")]
        ranked = feed.rank_routes(routes, {}, limit=0)
        self.assertEqual([r["route"][2] for r in ranked], ["ETH", "BNB", "SOL"])

    def test_a_route_missing_one_leg_volume_sorts_below_a_measured_one(self):
        tickers = {"BTC/USDT": {"quoteVolume": 10},
                   "ETH/BTC": {"quoteVolume": 10},
                   "ETH/USDT": {"quoteVolume": 10}}
        ranked = feed.rank_routes([route("BTC", "BNB"), route("BTC", "ETH")], tickers)
        self.assertEqual(ranked[0]["route"][2], "ETH")

    def test_base_volume_is_converted_when_quote_volume_is_absent(self):
        tickers = {"BTC/USDT": {"baseVolume": 2, "last": 10_000},
                   "ETH/BTC": {"baseVolume": 100, "last": 0.05},
                   "ETH/USDT": {"baseVolume": 30, "last": 500}}
        self.assertAlmostEqual(
            feed.route_volume(route("BTC", "ETH"), tickers), 5.0, places=9)

    def test_symbols_for_routes_deduplicates_in_order(self):
        symbols = feed.symbols_for_routes([route("BTC", "ETH"), route("BTC", "BNB")])
        self.assertEqual(symbols, ["BTC/USDT", "ETH/BTC", "ETH/USDT",
                                   "BNB/BTC", "BNB/USDT"])


class BulkVenue:
    """A venue that prices everything in one request."""

    has = {"fetchTickers": True}

    def __init__(self, tickers, reject_symbol_list=False):
        self.tickers = tickers
        self.reject_symbol_list = reject_symbol_list
        self.calls = []

    def fetch_tickers(self, symbols=None):
        self.calls.append(symbols)
        if symbols is not None and self.reject_symbol_list:
            raise RuntimeError("this venue does not accept a symbol list")
        return dict(self.tickers)


class SerialVenue:
    """A venue with no fetchTickers at all."""

    has = {"fetchTickers": False}

    def __init__(self, tickers, missing=()):
        self.tickers = tickers
        self.missing = set(missing)
        self.calls = []

    def fetch_ticker(self, symbol):
        self.calls.append(symbol)
        if symbol in self.missing:
            raise RuntimeError(f"{symbol} is not listed here")
        return dict(self.tickers[symbol])


class TestFetchStrategies(unittest.TestCase):

    def test_a_bulk_venue_is_priced_in_one_request(self):
        venue = BulkVenue({f"C{index}/USDT": ticker(1.0, 1.1)
                           for index in range(500)})
        symbols = list(venue.tickers)
        quotes, rejected = feed.fetch_bulk_quotes(venue, symbols, now_ms=NOW)
        self.assertEqual(len(venue.calls), 1)
        self.assertEqual(len(quotes), 500)
        self.assertEqual(rejected, {})

    def test_a_venue_that_rejects_a_symbol_list_is_asked_again_unfiltered(self):
        venue = BulkVenue({"BTC/USDT": ticker(100.0, 100.5),
                           "JUNK/USDT": ticker(1.0, 2.0)},
                          reject_symbol_list=True)
        quotes, _ = feed.fetch_bulk_quotes(venue, ["BTC/USDT"], now_ms=NOW)
        self.assertEqual(venue.calls, [["BTC/USDT"], None])
        self.assertEqual(list(quotes), ["BTC/USDT"])

    def test_a_serial_venue_records_unlisted_symbols_instead_of_failing(self):
        venue = SerialVenue({"BTC/USDT": ticker(100.0, 100.5),
                             "DOGE/USDT": ticker(0.1, 0.11)},
                            missing=["DOGE/USDT"])
        quotes, rejected = feed.fetch_quotes_one_by_one(
            venue, ["BTC/USDT", "DOGE/USDT"], now_ms=NOW)
        self.assertEqual(list(quotes), ["BTC/USDT"])
        self.assertIn("unavailable", rejected["DOGE/USDT"])

    def test_capability_detection_prefers_the_advertised_flag(self):
        self.assertTrue(feed.supports_bulk_tickers(BulkVenue({})))
        self.assertFalse(feed.supports_bulk_tickers(SerialVenue({})))

    def test_a_venue_without_the_has_dict_is_judged_by_its_methods(self):
        class Bare:
            def fetch_tickers(self, symbols=None):
                return {}

        self.assertTrue(feed.supports_bulk_tickers(Bare()))
        self.assertFalse(feed.supports_bulk_tickers(object()))


if __name__ == "__main__":
    unittest.main()
