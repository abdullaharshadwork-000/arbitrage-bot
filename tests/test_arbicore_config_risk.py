"""Configuration and risk: the two modules that decide whether an order happens.

`config` is where a typo has to become an error rather than a silently ignored
setting, and where "real money" has to be an explicit act. `risk` is what stops
the loop after the loss, rather than after the operator notices.
"""

import unittest
from decimal import Decimal

from arbicore.config import (ABSOLUTE_MAX_TRADE_SIZE_USDT, EXECUTION_PAPER,
                             EXECUTION_REAL, MODE_DEMO, MODE_LIVE,
                             REAL_TRADING_ACK, STRATEGY_TRIANGULAR,
                             Credentials, Settings, load_credentials)
from arbicore.money import ZERO
from arbicore.risk import (ALLOWED, HALT_CONSECUTIVE, HALT_DAILY_LOSS,
                           HALT_DRAWDOWN, HALT_KILL, HALT_STRANDED,
                           RiskManager)

SECRET = "0123456789abcdef"


def live_real(**overrides):
    """A Settings that would really trade, for testing the gates around it."""
    base = dict(mode=MODE_LIVE, execution_mode=EXECUTION_REAL,
                exchanges=("binance", "kucoin"),
                real_trading_ack=REAL_TRADING_ACK)
    base.update(overrides)
    settings = Settings(**base)
    return settings.with_credentials({
        name: Credentials(name, "key-" + name, SECRET, source="test")
        for name in settings.exchanges})


class TestCredentials(unittest.TestCase):

    def test_a_secret_never_appears_in_any_rendering(self):
        cred = Credentials("binance", "AKIAEXAMPLEKEY123", SECRET, "passphrase")
        for text in (repr(cred), str(cred), f"{cred}", str(cred.redacted())):
            self.assertNotIn(SECRET, text)
            self.assertNotIn("passphrase", text)
        # The key is masked to a tail only, which is enough to tell two keys
        # apart in a log without disclosing either.
        self.assertEqual(cred.redacted()["api_key"], "***Y123")

    def test_short_values_are_masked_entirely(self):
        self.assertEqual(Credentials("x", "abc", "d").redacted()["api_key"], "***")

    def test_ccxt_params_is_the_only_place_the_real_values_come_back(self):
        cred = Credentials("kucoin", "k", SECRET, "phrase")
        self.assertEqual(cred.ccxt_params(),
                         {"apiKey": "k", "secret": SECRET, "password": "phrase"})
        # No passphrase means no key at all, rather than an empty one ccxt
        # would sign with.
        self.assertNotIn("password", Credentials("binance", "k", SECRET)
                         .ccxt_params())

    def test_completeness_names_what_is_missing(self):
        self.assertEqual(Credentials("binance").missing(),
                         ["api_key", "api_secret"])
        self.assertEqual(Credentials("binance", "k").missing(), ["api_secret"])
        self.assertTrue(Credentials("binance", "k", "s").complete)

    def test_env_vars_follow_one_documented_pattern(self):
        env = {"ARBI_BINANCE_API_KEY": "  k  ",
               "ARBI_BINANCE_API_SECRET": SECRET,
               "ARBI_BINANCE_PASSWORD": "p"}
        cred = Credentials.from_env("binance", env)
        self.assertEqual(cred.api_key, "k")          # whitespace stripped
        self.assertEqual(cred.source, "env")
        self.assertEqual(Credentials.from_env("nobody", env).source, "unset")

    def test_a_hyphenated_exchange_id_maps_to_a_legal_env_name(self):
        env = {"ARBI_GATE_IO_API_KEY": "k", "ARBI_GATE_IO_API_SECRET": SECRET}
        self.assertTrue(Credentials.from_env("gate.io", env).complete)

    def test_the_environment_beats_an_in_process_mapping(self):
        # A developer's live_config.py must not override what an operator set
        # for the deployment.
        env = {"ARBI_BINANCE_API_KEY": "from-env",
               "ARBI_BINANCE_API_SECRET": SECRET}
        resolved = load_credentials(
            ["binance", "kucoin"],
            fallback={"binance": {"apiKey": "from-file", "secret": SECRET},
                      "kucoin": {"apiKey": "kc", "secret": SECRET}},
            environ=env)
        self.assertEqual(resolved["binance"].api_key, "from-env")
        self.assertEqual(resolved["binance"].source, "env")
        # With nothing in the environment the file is still used.
        self.assertEqual(resolved["kucoin"].api_key, "kc")
        self.assertEqual(resolved["kucoin"].source, "config")

    def test_ccxt_style_and_snake_case_mappings_both_load(self):
        for mapping in ({"apiKey": "k", "secret": SECRET},
                        {"api_key": "k", "api_secret": SECRET}):
            self.assertTrue(Credentials.from_mapping("binance", mapping).complete)


class TestSettings(unittest.TestCase):

    def test_the_shipped_defaults_are_valid(self):
        # A default configuration that fails its own validator is a bug in the
        # validator, which is exactly what happened once.
        self.assertEqual(Settings().validate(), [])

    def test_strings_are_coerced_at_construction_not_at_first_comparison(self):
        # Settings(max_daily_loss_usdt="20") holding a str raised a TypeError
        # from inside a risk check - the worst place to learn about it.
        settings = Settings(max_daily_loss_usdt="20", check_interval="3",
                            halt_on_stranded_position="yes",
                            symbols="BTC/USDT, ETH/USDT")
        self.assertEqual(settings.max_daily_loss_usdt, Decimal("20"))
        self.assertEqual(settings.check_interval, 3)
        self.assertIs(settings.halt_on_stranded_position, True)
        self.assertEqual(settings.symbols, ("BTC/USDT", "ETH/USDT"))
        self.assertGreater(settings.max_daily_loss_usdt, ZERO)   # no TypeError

    def test_real_trading_needs_all_three_switches(self):
        self.assertTrue(live_real().real)
        self.assertFalse(live_real(real_trading_ack="").real)
        self.assertFalse(live_real(real_trading_ack="i accept real losses").real)
        self.assertFalse(Settings(execution_mode=EXECUTION_REAL,
                                  mode=MODE_DEMO).real)
        self.assertTrue(Settings().paper)

    def test_real_execution_without_keys_is_a_blocking_error(self):
        naked = Settings(mode=MODE_LIVE, execution_mode=EXECUTION_REAL,
                         real_trading_ack=REAL_TRADING_ACK,
                         exchanges=("binance", "kucoin"))
        problems = "; ".join(naked.validate())
        self.assertIn("binance is missing api_key", problems)
        self.assertIn("ARBI_BINANCE_API_SECRET", problems)   # tells you the fix

    def test_real_execution_on_demo_data_is_refused(self):
        problems = "; ".join(live_real(mode=MODE_DEMO).validate())
        self.assertIn("real execution requires live market data", problems)

    def test_the_acknowledgement_string_must_match_exactly(self):
        problems = "; ".join(live_real(real_trading_ack="yes").validate())
        self.assertIn(REAL_TRADING_ACK, problems)

    def test_a_trade_size_above_the_hard_cap_is_refused(self):
        big = ABSOLUTE_MAX_TRADE_SIZE_USDT + 1
        problems = "; ".join(Settings(trade_size_usdt=big,
                                      max_position_notional_usdt=big * 2)
                             .validate())
        self.assertIn("hard cap", problems)

    def test_incoherent_limits_are_caught_as_a_set(self):
        problems = "; ".join(Settings(trade_size_usdt="500",
                                      max_position_notional_usdt="100").validate())
        self.assertIn("500.0 exceeds max_position_notional_usdt 100.0", problems)
        # Below the exchange minimum is the mirror error.
        self.assertIn("minimum most exchanges enforce",
                      "; ".join(Settings(trade_size_usdt="5").validate()))

    def test_cross_exchange_with_one_venue_is_not_arbitrage(self):
        self.assertIn("at least two exchanges",
                      "; ".join(Settings(exchanges=("binance",)).validate()))
        # Triangular is fine on one venue - that is the whole point of it.
        self.assertEqual(Settings(exchanges=("binance",),
                                  strategy=STRATEGY_TRIANGULAR).validate(), [])

    def test_a_zero_net_profit_floor_is_refused(self):
        self.assertIn("min_profit_pct must be positive",
                      "; ".join(Settings(min_profit_pct="0").validate()))

    def test_a_net_floor_thinner_than_the_fees_warns_but_does_not_block(self):
        # min_profit_pct is a *net* floor: fees are already subtracted, so
        # comparing it to the round-trip fee is advice, not an error.
        settings = Settings()          # 0.15% floor, 0.2% round trip
        self.assertEqual(settings.validate(), [])
        notes = "; ".join(settings.warnings())
        self.assertIn("round-trip fee", notes)
        self.assertIn("net profit floor", notes)

    def test_real_trading_without_an_alert_channel_warns(self):
        self.assertIn("go unnoticed", "; ".join(live_real().warnings()))
        quiet = live_real(alert_webhook="https://example.invalid/hook")
        self.assertNotIn("go unnoticed", "; ".join(quiet.warnings()))

    def test_zero_latency_paper_fills_warn_about_being_the_old_simulator(self):
        self.assertIn("optimistic", "; ".join(Settings(latency_ms=0).warnings()))

    def test_updated_validates_the_result_so_paired_changes_are_allowed(self):
        settings = Settings()
        raised = settings.updated(trade_size_usdt="800",
                                  max_position_notional_usdt="1000")
        self.assertEqual(raised.trade_size_usdt, Decimal("800"))
        # The same change alone leaves the config incoherent.
        with self.assertRaises(ValueError):
            settings.updated(trade_size_usdt="800")
        # And the original is untouched, because Settings is immutable.
        self.assertEqual(settings.trade_size_usdt, Decimal("200"))

    def test_an_unknown_setting_is_an_error_not_a_silent_no_op(self):
        with self.assertRaises(KeyError):
            Settings().updated(mn_profit_pct="0.3")
        with self.assertRaises(KeyError):
            Settings.coerce("credentials", {})

    def test_a_non_numeric_value_names_the_field_it_came_from(self):
        with self.assertRaises(ValueError) as caught:
            Settings().updated(trade_size_usdt="two hundred")
        self.assertIn("trade_size_usdt must be a number", str(caught.exception))

    def test_as_dict_is_json_safe_and_leaks_nothing(self):
        payload = live_real().as_dict()
        self.assertNotIn(SECRET, str(payload))
        self.assertTrue(payload["real_trading_enabled"])
        self.assertIsInstance(payload["trade_size_usdt"], float)
        # The webhook itself is a credential, so only its presence is reported.
        self.assertNotIn("alert_webhook", payload)
        self.assertIs(payload["alert_webhook_configured"], False)
        self.assertEqual(payload["credentials"]["binance"]["api_secret"], "***")

    def test_from_mapping_ignores_keys_that_are_not_settings(self):
        settings = Settings.from_mapping({"trade_size_usdt": "300",
                                          "nonsense": 1, "credentials": {}})
        self.assertEqual(settings.trade_size_usdt, Decimal("300"))

    def test_blocking_reason_is_one_sentence_or_none(self):
        self.assertIsNone(Settings().blocking_reason())
        self.assertIsInstance(Settings(min_profit_pct="0").blocking_reason(), str)


class Clock:
    """A clock the test moves by hand."""

    def __init__(self, start=1_700_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


def manager(**overrides):
    base = dict(max_daily_loss_usdt="20", max_position_notional_usdt="400",
                max_consecutive_failures=3, max_orders_per_minute=3,
                trade_size_usdt="200")
    base.update(overrides)
    clock = Clock()
    return RiskManager(Settings(**base), clock=clock), clock


class TestRiskManager(unittest.TestCase):

    def test_a_normal_trade_is_allowed(self):
        risk, _clock = manager()
        decision = risk.check("200", "BTC/USDT")
        self.assertTrue(decision)
        self.assertTrue(bool(ALLOWED))

    def test_an_oversized_notional_is_refused_naming_the_limit(self):
        risk, _clock = manager()
        decision = risk.check("500", "BTC/USDT")
        self.assertFalse(decision)
        self.assertEqual(decision.limit, "position_notional")
        self.assertIn("per-position cap", decision.reason)
        self.assertIn("BTC/USDT", decision.reason)
        # A refused size is a veto, not a halt: nothing was risked.
        self.assertFalse(risk.halted)

    def test_the_order_rate_cap_trips_and_then_clears(self):
        risk, clock = manager()
        for _ in range(3):
            self.assertTrue(risk.check("200"))
            risk.record_order()
        self.assertFalse(risk.check("200"))
        clock.advance(61)
        self.assertTrue(risk.check("200"))

    def test_consecutive_failures_latch_a_halt(self):
        risk, _clock = manager()
        for index in range(3):
            risk.record_failure(f"venue timeout {index}")
        self.assertTrue(risk.halted)
        self.assertEqual(risk.halt_limit, HALT_CONSECUTIVE)
        self.assertFalse(risk.check("200"))

    def test_a_win_clears_the_failure_streak(self):
        risk, _clock = manager()
        risk.record_failure("one")
        risk.record_failure("two")
        risk.record_success(Decimal("0.5"))
        self.assertEqual(risk.consecutive_failures, 0)
        risk.record_failure("three")
        self.assertFalse(risk.halted)

    def test_a_losing_trade_counts_toward_the_streak_rather_than_clearing_it(self):
        # A string of small losses is still the venue telling you something, so
        # a negative round trip is treated exactly like a failed one.
        risk, _clock = manager()
        risk.record_failure("one")
        risk.record_success(Decimal("-0.5"))
        self.assertEqual(risk.consecutive_failures, 2)
        self.assertEqual(risk.losses_today, 1)

    def test_the_daily_loss_limit_halts_on_the_trade_that_breaches_it(self):
        risk, _clock = manager()
        risk.record_success(Decimal("-28.50"))
        self.assertTrue(risk.halted)
        self.assertEqual(risk.halt_limit, HALT_DAILY_LOSS)
        self.assertGreaterEqual(risk.snapshot()["daily_loss_used_pct"], 100.0)

    def test_a_halt_survives_utc_midnight_even_as_the_day_resets(self):
        risk, clock = manager()
        risk.record_success(Decimal("-25"))
        self.assertTrue(risk.halted)
        clock.advance(86_400)
        risk.check("200")                       # forces the day roll
        self.assertEqual(risk.realized_today, ZERO)
        self.assertTrue(risk.halted)            # a broken venue is still broken

    def test_a_drawdown_from_peak_equity_halts_at_the_next_check(self):
        # update_equity only records; the latch happens on the next pre-trade
        # check, which is always before an order goes out.
        risk, _clock = manager()
        risk.update_equity(Decimal("10000"))
        risk.update_equity(Decimal("9000"))     # 1000 below the peak
        self.assertFalse(risk.halted)
        decision = risk.check("200")
        self.assertFalse(decision)
        self.assertTrue(risk.halted)
        self.assertEqual(risk.halt_limit, HALT_DRAWDOWN)
        self.assertIn("below its peak", risk.halt_reason)

    def test_equity_recovering_to_a_new_peak_does_not_halt(self):
        risk, _clock = manager()
        risk.update_equity(Decimal("10000"))
        risk.update_equity(Decimal("10500"))
        self.assertTrue(risk.check("200"))

    def test_a_stranded_position_halts_and_blocks_resume_until_cleared(self):
        risk, _clock = manager()
        risk.record_stranded("binance", "ETH", Decimal("0.05"),
                             "third leg had no book")
        self.assertTrue(risk.halted)
        self.assertEqual(risk.halt_limit, HALT_STRANDED)
        ok, reason = risk.resume()
        self.assertFalse(ok)
        self.assertIn("stranded", reason)
        risk.clear_stranded()
        ok, _reason = risk.resume()
        self.assertTrue(ok)
        self.assertTrue(risk.check("200"))

    def test_the_kill_switch_cannot_be_resumed_in_process(self):
        risk, _clock = manager()
        risk.kill("operator pulled the plug")
        self.assertEqual(risk.halt_limit, HALT_KILL)
        ok, reason = risk.resume()
        self.assertFalse(ok)
        self.assertIn("restart the process", reason)

    def test_snapshot_is_json_safe(self):
        risk, _clock = manager()
        risk.record_success(Decimal("-1.25"))
        payload = risk.snapshot()
        self.assertIsInstance(payload["realized_today"], float)
        self.assertIsInstance(payload["halted"], bool)


if __name__ == "__main__":
    unittest.main(verbosity=2)
