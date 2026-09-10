"""Unattended operation: telling someone, and knowing what state you woke into.

`alerts` must never be the reason a run dies - a notifier that raises while
reporting a halt has made the halt worse. `reconcile` must never say "safe to
start" about an account it could not read. `rebalance` has to make the cost of
moving inventory arithmetic instead of an assumption.
"""

import unittest
from decimal import Decimal

from arbicore import alerts, reconcile
from arbicore.ledger import Ledger
from arbicore.money import ZERO
from arbicore.orders import OrderReconciliationError
from arbicore.rebalance import (FALLBACK_NETWORK, RebalanceSimulator, Transfer,
                                network_for, skew_targets, trades_to_repay)

TELEGRAM = "https://api.telegram.org/bot123456:AAHsecrettoken/sendMessage"


class Recorder:
    """Captures what would have been posted, in place of the network."""

    def __init__(self, raises=None):
        self.calls = []
        self.raises = raises

    def __call__(self, title, body, severity):
        self.calls.append((title, body, severity))
        if self.raises:
            raise self.raises


class TestRedactUrl(unittest.TestCase):

    def test_a_webhook_url_is_itself_a_credential(self):
        redacted = alerts.redact_url(TELEGRAM)
        self.assertNotIn("AAHsecrettoken", redacted)
        self.assertNotIn("123456", redacted)
        self.assertIn("api.telegram.org", redacted)

    def test_slack_and_empty_urls_survive(self):
        self.assertNotIn(
            "T00000", alerts.redact_url(
                "https://hooks.slack.com/services/T00000/B00000/XXXX"))
        self.assertEqual(alerts.redact_url(""), "")

    def test_a_malformed_url_degrades_instead_of_raising(self):
        self.assertIsInstance(alerts.redact_url("not a url at all"), str)


class TestPayloadShaping(unittest.TestCase):

    def test_each_service_gets_the_field_it_requires(self):
        self.assertIn("text", alerts._payload_for(
            "https://hooks.slack.com/services/x", "t", "b", alerts.WARNING))
        self.assertIn("content", alerts._payload_for(
            "https://discord.com/api/webhooks/x", "t", "b", alerts.WARNING))
        self.assertIn("disable_web_page_preview", alerts._payload_for(
            TELEGRAM, "t", "b", alerts.WARNING))
        generic = alerts._payload_for("https://example.invalid/hook", "t", "b",
                                      alerts.CRITICAL)
        self.assertEqual(generic["severity"], alerts.CRITICAL)

    def test_discord_is_truncated_to_its_own_limit(self):
        payload = alerts._payload_for("https://discord.com/api/webhooks/x",
                                      "t", "b" * 5000, alerts.WARNING)
        self.assertLessEqual(len(payload["content"]), 1900)

    def test_badges_are_ascii_because_the_windows_console_is_cp1252(self):
        # An emoji badge raised UnicodeEncodeError on the way to reporting a
        # halt, which is strictly worse than no badge.
        for badge in alerts.BADGES.values():
            badge.encode("cp1252")
            self.assertTrue(badge.isascii())


class TestNotifier(unittest.TestCase):

    def notifier(self, **overrides):
        options = dict(sender=Recorder(), console=None, async_send=False,
                       clock=lambda: self.now[0])
        options.update(overrides)
        return alerts.Notifier(**options), options["sender"]

    def setUp(self):
        self.now = [1000.0]

    def test_an_alert_reaches_the_sender(self):
        notifier, sender = self.notifier()
        self.assertTrue(notifier.send("Trading halted", "daily loss",
                                      alerts.CRITICAL))
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(notifier.sent, 1)

    def test_a_repeated_condition_alerts_once_per_cooloff(self):
        notifier, sender = self.notifier(cooloff=300)
        notifier.halted("daily_loss_limit", "down 25 USDT")
        notifier.halted("daily_loss_limit", "down 25 USDT")
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(notifier.suppressed, 1)
        self.now[0] += 301
        notifier.halted("daily_loss_limit", "down 25 USDT")
        self.assertEqual(len(sender.calls), 2)

    def test_a_different_limit_is_a_different_alert(self):
        notifier, sender = self.notifier()
        notifier.halted("daily_loss_limit", "a")
        notifier.halted("consecutive_failures", "b")
        self.assertEqual(len(sender.calls), 2)

    def test_severity_below_the_floor_is_dropped(self):
        notifier, sender = self.notifier(min_severity=alerts.CRITICAL)
        notifier.send("routine", "nothing", alerts.INFO)
        self.assertEqual(sender.calls, [])
        notifier.send("bad", "something", alerts.CRITICAL)
        self.assertEqual(len(sender.calls), 1)

    def test_a_failing_transport_is_recorded_never_raised(self):
        # The alert path runs while the bot is already in trouble; it must not
        # add an exception to that.
        notifier, _sender = self.notifier(
            sender=Recorder(raises=OSError("dns failure")))
        notifier.send("Trading halted", "reason", alerts.CRITICAL)
        self.assertEqual(notifier.failed, 1)
        self.assertIn("dns failure", notifier.last_error)
        self.assertEqual(notifier.sent, 0)

    def test_a_broken_console_does_not_break_the_alert(self):
        def bad_console(_line):
            raise UnicodeEncodeError("charmap", "x", 0, 1, "cp1252")

        notifier, sender = self.notifier(console=bad_console)
        notifier.send("Trading halted", "reason", alerts.CRITICAL)
        self.assertEqual(len(sender.calls), 1)

    def test_with_no_webhook_the_console_is_still_told(self):
        lines = []
        notifier = alerts.Notifier(webhook_url="", console=lines.append,
                                   async_send=False)
        self.assertFalse(notifier.configured)
        self.assertFalse(notifier.send("Trading halted", "daily loss",
                                       alerts.CRITICAL))
        self.assertEqual(len(lines), 1)
        self.assertIn("[HALT]", lines[0])

    def test_the_log_is_bounded(self):
        notifier, _sender = self.notifier()
        for index in range(150):
            notifier.send(f"alert {index}", "", alerts.WARNING)
        self.assertEqual(len(notifier.log), 100)

    def test_an_unreconciled_fill_alert_carries_the_ids_to_investigate(self):
        notifier, sender = self.notifier()
        error = OrderReconciliationError("could not confirm", exchange="binance",
                                         symbol="BTC/USDT", order_id="77",
                                         client_order_id="arbi-abc")
        notifier.unreconciled(error)
        title, body, severity = sender.calls[0]
        self.assertEqual(severity, alerts.CRITICAL)
        self.assertIn("77", body)
        self.assertIn("arbi-abc", body)
        self.assertTrue(title.isascii())

    def test_domain_helpers_each_produce_one_alert(self):
        notifier, sender = self.notifier()
        notifier.stranded("binance", "ETH", Decimal("0.05"), "third leg died")
        notifier.resumed()
        notifier.losing_trade(type("T", (), {
            "symbol": "BTC/USDT", "expected_profit": Decimal("0.3"),
            "realized_profit": Decimal("-0.1")})())
        self.assertEqual(len(sender.calls), 3)
        self.assertIn("0.05 ETH on binance", sender.calls[0][1])

    def test_snapshot_never_contains_the_raw_webhook(self):
        notifier = alerts.Notifier(webhook_url=TELEGRAM, console=None,
                                   async_send=False)
        payload = notifier.snapshot()
        self.assertNotIn("AAHsecrettoken", str(payload))
        self.assertTrue(payload["configured"])


class FakeVenue:
    """A ccxt-shaped client for reconciliation tests."""

    def __init__(self, open_orders=None, balance=None, server_time=None,
                 fail_orders=False, fail_balance=False):
        self._open = list(open_orders or [])
        self._balance = balance
        self._time = server_time
        self.fail_orders = fail_orders
        self.fail_balance = fail_balance
        self.cancelled = []

    def fetch_open_orders(self, symbol=None):
        if self.fail_orders:
            raise ConnectionError("venue unreachable")
        return [order for order in self._open
                if symbol is None or order.get("symbol") == symbol]

    def fetch_balance(self):
        if self.fail_balance:
            raise ConnectionError("balance endpoint down")
        return {"free": self._balance or {}}

    def cancel_order(self, order_id, symbol=None):
        self.cancelled.append((order_id, symbol))
        return {"id": order_id, "status": "canceled"}

    def fetch_time(self):
        if self._time is None:
            raise NotImplementedError
        return self._time


RESTING = {"id": "88", "symbol": "BTC/USDT", "side": "buy", "amount": 0.5,
           "filled": 0.2, "price": 99000.0, "status": "open"}


class TestBalanceDrift(unittest.TestCase):

    def test_dust_is_ignored_so_the_report_stays_worth_reading(self):
        drifts = reconcile.balance_drift({"USDT": 1000}, {"USDT": Decimal("1000.3")},
                                         prices={})
        self.assertEqual(drifts, [])

    def test_a_real_gap_is_reported_with_its_value(self):
        drifts = reconcile.balance_drift({"BTC": 1}, {"BTC": Decimal("0.5")},
                                         prices={"BTC": 100000})
        self.assertEqual(len(drifts), 1)
        self.assertEqual(drifts[0]["gap"], Decimal("-0.5"))
        self.assertEqual(drifts[0]["gap_value_quote"], Decimal("50000"))

    def test_a_currency_the_bot_did_not_know_about_shows_up(self):
        drifts = reconcile.balance_drift({}, {"BNB": Decimal("2")},
                                         prices={"BNB": 600})
        self.assertEqual(drifts[0]["currency"], "BNB")
        self.assertIsNone(drifts[0]["gap_pct"])       # no baseline to compare

    def test_an_unpriced_currency_is_reported_rather_than_silently_dropped(self):
        drifts = reconcile.balance_drift({"MYSTERY": 0}, {"MYSTERY": Decimal("5")},
                                         prices={})
        self.assertEqual(len(drifts), 1)
        self.assertIsNone(drifts[0]["gap_value_quote"])

    def test_a_gap_inside_the_percentage_tolerance_is_ignored(self):
        drifts = reconcile.balance_drift({"USDT": 10000},
                                         {"USDT": Decimal("10050")}, prices={},
                                         tolerance_pct=Decimal("1.0"))
        self.assertEqual(drifts, [])


class TestStartupCheck(unittest.TestCase):

    def test_a_clean_account_is_reported_clean(self):
        report = reconcile.startup_check({"binance": FakeVenue(balance={})},
                                         ["BTC/USDT"], expected_balances={},
                                         clock=lambda: 1000.0)
        self.assertTrue(report.clean)
        self.assertFalse(report.blocking)
        self.assertIn("nothing outstanding", report.summary())

    def test_a_resting_order_blocks_the_start_and_says_what_it_is(self):
        report = reconcile.startup_check({"binance": FakeVenue([RESTING])},
                                         ["BTC/USDT"], clock=lambda: 1000.0)
        self.assertTrue(report.blocking)
        found = report.of_kind("open_order")
        self.assertEqual(len(found), 1)
        self.assertIn("0.2/0.5 filled", found[0].detail)

    def test_an_open_order_outside_the_selected_symbols_still_blocks_start(self):
        outside = {**RESTING, "id": "99", "symbol": "SOL/USDT"}
        report = reconcile.startup_check(
            {"binance": FakeVenue([outside])}, ["BTC/USDT"],
            clock=lambda: 1000.0)
        self.assertTrue(report.blocking)
        self.assertEqual(report.of_kind("open_order")[0].data["symbol"],
                         "SOL/USDT")

    def test_an_unreachable_venue_is_not_reported_as_safe(self):
        # The failure mode this guards: blocking=False on an account nobody
        # could read is a green light to trade blind.
        report = reconcile.startup_check(
            {"binance": FakeVenue(fail_orders=True)}, ["BTC/USDT"],
            clock=lambda: 1000.0)
        self.assertEqual(report.discrepancies, ())
        self.assertTrue(report.errors)
        self.assertTrue(report.blocking)
        self.assertIn("unreachable", report.summary())

    def test_a_venue_that_cannot_report_balances_is_an_error_too(self):
        report = reconcile.startup_check(
            {"binance": FakeVenue(fail_balance=True)}, ["BTC/USDT"],
            expected_balances={"binance": {"USDT": 1000}}, clock=lambda: 1000.0)
        self.assertTrue(report.blocking)
        self.assertIn("could not read balances", report.errors[0])

    def test_the_bots_own_unfinished_trades_are_surfaced(self):
        report = reconcile.startup_check(
            {}, ["BTC/USDT"],
            pending_records=[{"exchange": "binance",
                              "reason": "sell leg never confirmed"}],
            clock=lambda: 1000.0)
        found = report.of_kind("unresolved_record")
        self.assertEqual(len(found), 1)
        self.assertIn("sell leg never confirmed", found[0].detail)
        self.assertTrue(report.blocking)

    def test_clock_skew_is_flagged_with_the_fix_rather_than_the_symptom(self):
        # The exchange error for this reads like an API-key problem, which
        # sends people down the wrong path.
        venue = FakeVenue(server_time=1_000_000_000_000)
        report = reconcile.startup_check(
            {"binance": venue}, ["BTC/USDT"],
            clock=lambda: 1_000_000_060.0, max_clock_skew_ms=2000)
        found = report.of_kind("clock_skew")
        self.assertEqual(len(found), 1)
        self.assertIn("resynced", found[0].detail)
        self.assertEqual(found[0].data["skew_ms"], 60_000)

    def test_a_venue_without_fetch_time_is_simply_not_checked(self):
        report = reconcile.startup_check({"kraken": FakeVenue()}, ["BTC/USDT"],
                                         clock=lambda: 1000.0)
        self.assertEqual(report.of_kind("clock_skew"), ())

    def test_balance_drift_blocks_the_start_until_inventory_is_reconciled(self):
        venue = FakeVenue(balance={"USDT": Decimal("500")})
        report = reconcile.startup_check(
            {"binance": venue}, ["BTC/USDT"],
            expected_balances={"binance": {"USDT": Decimal("1000")}},
            prices={}, clock=lambda: 1000.0)
        drift = report.of_kind("balance_drift")
        self.assertEqual(len(drift), 1)
        self.assertTrue(drift[0].blocking)
        self.assertTrue(report.blocking)

    def test_as_dict_is_json_safe(self):
        report = reconcile.startup_check({"binance": FakeVenue([RESTING])},
                                        ["BTC/USDT"], clock=lambda: 1000.0)
        payload = report.as_dict()
        self.assertIsInstance(payload["discrepancies"][0]["data"]["filled"], float)
        self.assertTrue(payload["blocking"])


class TestCancelAndUnwind(unittest.TestCase):

    def test_cancelling_does_nothing_without_an_explicit_confirmation(self):
        venue = FakeVenue([RESTING])
        result = reconcile.cancel_open_orders({"binance": venue}, ["BTC/USDT"])
        self.assertEqual(result["cancelled"], [])
        self.assertEqual(venue.cancelled, [])
        self.assertIn("confirm=True", result["note"])

    def test_confirmed_cancellation_warns_about_the_filled_part(self):
        venue = FakeVenue([RESTING])
        result = reconcile.cancel_open_orders({"binance": venue}, ["BTC/USDT"],
                                             confirm=True)
        self.assertEqual(venue.cancelled, [("88", "BTC/USDT")])
        self.assertEqual(result["cancelled"][0]["filled"], 0.2)
        self.assertIn("left a real position behind", result["note"])

    def test_a_cancel_that_fails_is_reported_not_swallowed(self):
        class Stubborn(FakeVenue):
            def cancel_order(self, order_id, symbol=None):
                raise RuntimeError("order already filled")

        result = reconcile.cancel_open_orders(
            {"binance": Stubborn([RESTING])}, ["BTC/USDT"], confirm=True)
        self.assertEqual(result["cancelled"], [])
        self.assertIn("order already filled", result["failed"][0]["error"])

    def test_an_unwind_is_described_never_executed(self):
        plan = reconcile.plan_unwind("binance", "ETH", "0.05",
                                     prices={"ETH": 4000})
        self.assertEqual(plan["symbol"], "ETH/USDT")
        self.assertEqual(plan["side"], "sell")
        self.assertEqual(plan["estimated_proceeds"], 200.0)
        self.assertIn("realizes the loss now", plan["note"])

    def test_an_unpriced_unwind_still_produces_a_usable_plan(self):
        plan = reconcile.plan_unwind("binance", "MYSTERY", "5")
        self.assertIsNone(plan["estimated_proceeds"])


class TestNetworkCosts(unittest.TestCase):

    def test_known_currencies_have_a_fee_a_delay_and_a_minimum(self):
        fee, minutes, minimum = network_for("BTC")
        self.assertEqual(fee, Decimal("0.0002"))
        self.assertEqual(minutes, 30)
        self.assertEqual(minimum, Decimal("0.001"))

    def test_an_unknown_currency_falls_back_rather_than_costing_nothing(self):
        self.assertEqual(network_for("WEIRDCOIN"), FALLBACK_NETWORK)

    def test_a_deployment_can_override_the_table(self):
        # TRC-20 USDT instead of ERC-20 is the single biggest lever here.
        fee, minutes, minimum = network_for(
            "USDT", {"USDT": (Decimal("1"), 2, Decimal("10"))})
        self.assertEqual((fee, minutes, minimum),
                         (Decimal("1"), 2, Decimal("10")))

    def test_trades_to_repay_is_the_number_that_decides_viability(self):
        # A 20 USDT withdrawal fee against a 0.30 USDT edge per trade.
        self.assertAlmostEqual(float(trades_to_repay("20", "200", "0.15")),
                               66.67, places=2)
        self.assertIsNone(trades_to_repay("20", "200", "0"))


class TestRebalanceSimulator(unittest.TestCase):

    def setUp(self):
        self.now = [1000.0]
        self.ledger = Ledger.funded(["A", "B"], {"USDT": 1000, "BTC": 1})
        self.sim = RebalanceSimulator(self.ledger, clock=lambda: self.now[0])

    def test_funds_leave_immediately_and_arrive_only_after_confirmation(self):
        transfer = self.sim.start("A", "B", "BTC", "0.5")
        self.assertTrue(transfer.ok)
        self.assertEqual(self.ledger.get("A", "BTC"), Decimal("0.5"))
        self.assertEqual(self.ledger.get("B", "BTC"), Decimal("1"))   # not yet
        # In-flight capital belongs to neither venue: this is the cost fees
        # alone do not capture.
        self.assertEqual(self.sim.in_flight({"BTC": 100000}), Decimal("50000"))
        self.assertEqual(self.sim.settle(), [])                       # too early
        self.now[0] += 30 * 60
        arrived = self.sim.settle()
        self.assertEqual(len(arrived), 1)
        # 0.5 minus the 0.0002 network fee.
        self.assertEqual(self.ledger.get("B", "BTC"), Decimal("1.4998"))
        self.assertEqual(self.sim.fees_paid["BTC"], Decimal("0.0002"))
        self.assertEqual(self.sim.in_flight({"BTC": 100000}), ZERO)

    def test_an_amount_below_the_network_minimum_cannot_be_fixed_by_a_transfer(self):
        transfer = self.sim.start("A", "B", "BTC", "0.0005")
        self.assertFalse(transfer.ok)
        self.assertIn("below the", transfer.reason)
        self.assertEqual(self.ledger.get("A", "BTC"), Decimal("1"))
        self.assertEqual(len(self.sim.refused), 1)

    def test_a_transfer_the_fee_would_consume_is_refused(self):
        sim = RebalanceSimulator(
            self.ledger,
            networks={"BTC": (Decimal("0.01"), 30, Decimal("0.001"))},
            clock=lambda: self.now[0])
        transfer = sim.start("A", "B", "BTC", "0.005")
        self.assertFalse(transfer.ok)
        self.assertIn("would consume", transfer.reason)

    def test_transferring_more_than_the_venue_holds_is_refused(self):
        transfer = self.sim.start("A", "B", "BTC", "5")
        self.assertFalse(transfer.ok)
        self.assertEqual(self.ledger.get("A", "BTC"), Decimal("1"))

    def test_degenerate_transfers_are_refused(self):
        self.assertFalse(self.sim.start("A", "A", "BTC", "0.5").ok)
        self.assertFalse(self.sim.start("A", "B", "BTC", "0").ok)

    def test_the_plan_stays_quiet_until_the_skew_trigger(self):
        plan = self.sim.plan("BTC/USDT", {"BTC": 500}, trigger="0.85")
        self.assertFalse(plan["needed"])
        self.assertIn("under the", plan["reason"])

    def test_a_drained_pair_of_venues_produces_a_costed_transfer(self):
        self.ledger.set_balance("A", "USDT", 0)      # A is all coin
        self.ledger.set_balance("B", "BTC", 0)       # B is all quote
        plan = self.sim.plan("BTC/USDT", {"BTC": 100000}, trade_size="200")
        self.assertTrue(plan["needed"])
        move = plan["transfers"][0]
        self.assertEqual((move["source"], move["destination"]), ("A", "B"))
        self.assertEqual(move["currency"], "BTC")
        self.assertEqual(move["minutes"], 30)
        self.assertEqual(move["cost_quote"], 20.0)   # 0.0002 BTC at 100k
        self.assertIn("cannot buy", move["why"])
        self.assertEqual(plan["total_cost_quote"], 20.0)

    def test_a_plan_with_no_balances_says_so_rather_than_dividing_by_zero(self):
        empty = RebalanceSimulator(Ledger(), clock=lambda: self.now[0])
        plan = empty.plan("BTC/USDT", {"BTC": 100000})
        self.assertFalse(plan["needed"])
        self.assertIn("no balances", plan["reason"])

    def test_an_unpriced_currency_has_an_unknown_cost_not_a_free_one(self):
        detail = self.sim.cost_of({"source": "A", "destination": "B",
                                   "currency": "MYSTERY", "quantity": "10"})
        self.assertIsNone(detail["cost_quote"])
        self.assertTrue(detail["below_minimum"] is False)

    def test_skew_targets_names_both_ends_of_the_transfer(self):
        self.ledger.set_balance("A", "USDT", 0)
        self.ledger.set_balance("B", "BTC", 0)
        skew, coin_heavy, quote_heavy = skew_targets(
            self.ledger, "BTC/USDT", {"BTC": 100000})
        self.assertEqual((coin_heavy, quote_heavy), ("A", "B"))
        self.assertEqual(skew["A"], 1.0)
        self.assertEqual(skew["B"], 0.0)

    def test_snapshot_is_json_safe_and_keeps_the_last_refusals(self):
        self.sim.start("A", "B", "BTC", "0.0005")     # refused
        self.sim.start("A", "B", "BTC", "0.5")        # pending
        payload = self.sim.snapshot({"BTC": 100000})
        self.assertEqual(len(payload["pending"]), 1)
        self.assertEqual(len(payload["refused"]), 1)
        self.assertIsInstance(payload["in_flight_quote"], float)
        self.assertEqual(payload["completed"], 0)

    def test_a_transfer_records_when_it_will_arrive(self):
        transfer = Transfer("A", "B", "ETH", Decimal("1"), Decimal("0.0015"), 6,
                            started_at=1000.0)
        self.assertEqual(transfer.arrives_at, 1000.0 + 360)
        self.assertEqual(transfer.credited, Decimal("0.9985"))
        self.assertIsInstance(transfer.as_dict()["credited"], float)

    def test_credited_never_goes_negative(self):
        swallowed = Transfer("A", "B", "XRP", Decimal("0.1"), Decimal("0.2"), 1)
        self.assertEqual(swallowed.credited, ZERO)


if __name__ == "__main__":
    unittest.main(verbosity=2)
