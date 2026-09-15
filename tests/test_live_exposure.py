"""Offline account-risk fixtures: this module never constructs an exchange."""

from copy import deepcopy
from decimal import Decimal, localcontext
import json

import pytest

from arbicore.exposure import snapshot


def asset(free="0", used="0", price="1"):
    free, used, price = map(Decimal, (str(free), str(used), str(price)))
    return {"free": str(free), "used": str(used), "total": str(free + used),
            "value_free_usdt": str(free * price),
            "value_used_usdt": str(used * price)}


def valuation(balances):
    free = sum((Decimal(row["value_free_usdt"])
                for accounts in balances.values() for row in accounts.values()), Decimal(0))
    used = sum((Decimal(row["value_used_usdt"])
                for accounts in balances.values() for row in accounts.values()), Decimal(0))
    return {"free_usdt": str(free), "used_usdt": str(used), "total_usdt": str(free + used)}


def funded():
    return {"binance": {"USDT": asset(700), "BTC": asset("0.003", price=100000)}}


def test_every_non_usdt_holding_and_locked_inventory_counts_across_venues():
    balances = {
        "binance": {"USDT": asset(600, 100), "BTC": asset("0.001", "0.002", 100000)},
        "kucoin": {"USDT": asset(700), "USDC": asset(100), "ETH": asset(0, "0.1", 2000)},
    }
    decision = snapshot(balances, valuation(balances), 50, expected_exchanges=list(balances))
    assert decision["allowed"]
    assert decision["reason"] == ""
    assert decision["total_inventory_usdt"] == 600
    assert decision["total_equity_usdt"] == 2000
    assert decision["exposure_pct"] == decision["projected_exposure_pct"] == 30
    assert decision["headroom_usdt"] == 400


def test_first_leg_is_added_without_credit_for_a_future_sell_or_extra_equity():
    balances = funded()
    decision = snapshot(balances, valuation(balances), 40, pending_notional=101)
    assert not decision["allowed"]
    assert decision["total_inventory_usdt"] == 300
    assert decision["exposure_pct"] == 30
    assert decision["projected_exposure_pct"] == 40.1
    assert decision["headroom_usdt"] == 100
    assert "exceeds" in decision["reason"]


def test_decimal_boundary_is_checked_before_converting_to_float():
    balances = funded()
    totals = valuation(balances)
    with localcontext() as context:
        context.prec = 6
        assert snapshot(balances, totals, 40, "100")["allowed"]
        above = snapshot(balances, totals, 40, "100.000000000000000000001")
    assert not above["allowed"]
    # These round to the same display float; the allow/refuse decision does not.
    assert above["projected_exposure_pct"] == 40.0


def test_an_already_exceeded_cap_has_no_headroom_even_without_a_new_order():
    balances = funded()
    decision = snapshot(balances, valuation(balances), 20)
    assert not decision["allowed"]
    assert decision["headroom_usdt"] == 0


@pytest.mark.parametrize("missing", ["value_free_usdt", "value_used_usdt"])
def test_positive_holdings_with_missing_marks_fail_closed(missing):
    balances = {"binance": {"USDT": asset(900), "TOKEN": asset(1, 1, 50)}}
    totals = valuation(balances)
    del balances["binance"]["TOKEN"][missing]
    decision = snapshot(balances, totals, 100)
    assert not decision["allowed"]
    assert "TOKEN" in decision["reason"]
    assert decision["total_inventory_usdt"] is None
    assert decision["headroom_usdt"] == 0


def test_unlisted_asset_with_zero_price_is_not_treated_as_zero_exposure():
    balances = {"binance": {"USDT": asset(1000), "UNLISTED": asset(0, 20, 0)}}
    decision = snapshot(balances, valuation(balances), 100)
    assert not decision["allowed"]
    assert "Unvalued" in decision["reason"]
    assert "UNLISTED" in decision["reason"]


def test_zero_holdings_need_no_market_price():
    balances = {"binance": {"USDT": asset(1000), "UNLISTED": asset()}}
    totals = valuation(balances)
    del balances["binance"]["UNLISTED"]["value_free_usdt"]
    del balances["binance"]["UNLISTED"]["value_used_usdt"]
    decision = snapshot(balances, totals, 0)
    assert decision["allowed"]
    assert decision["total_inventory_usdt"] == 0


@pytest.mark.parametrize("value", [None, "invalid", "NaN", "Infinity", "-Infinity", -1, True])
@pytest.mark.parametrize("field", ["free", "used", "total", "value_free_usdt", "value_used_usdt"])
def test_invalid_or_nonfinite_balance_fields_never_authorize_an_entry(field, value):
    balances = funded()
    totals = valuation(balances)
    balances["binance"]["BTC"][field] = value
    decision = snapshot(balances, totals, 100)
    assert not decision["allowed"]
    assert decision["reason"]
    assert decision["headroom_usdt"] == 0


@pytest.mark.parametrize("balances", [None, {}, {"binance": {}}, {"binance": None},
                                     {"binance": {"error": "offline"}}])
def test_missing_or_failed_venue_is_not_a_zero_balance(balances):
    decision = snapshot(balances, {"free_usdt": 0, "used_usdt": 0, "total_usdt": 0}, 50)
    assert not decision["allowed"]
    assert decision["total_inventory_usdt"] is None


def test_omitted_expected_venue_fails_even_when_other_venue_is_healthy():
    balances = funded()
    decision = snapshot(balances, valuation(balances), 100,
                        expected_exchanges=["binance", "kucoin"])
    assert not decision["allowed"]
    assert "missing an expected venue" in decision["reason"]


@pytest.mark.parametrize("value", [None, "NaN", "Infinity", -1, "wrong", True])
@pytest.mark.parametrize("field", ["free_usdt", "used_usdt", "total_usdt"])
def test_unknown_or_nonfinite_summary_values_fail_closed(field, value):
    balances = funded()
    totals = valuation(balances)
    totals[field] = value
    assert not snapshot(balances, totals, 100)["allowed"]


def test_inflated_equity_summary_cannot_dilute_reported_exposure():
    balances = funded()
    totals = valuation(balances)
    totals["total_usdt"] = "1000000"
    decision = snapshot(balances, totals, 10)
    assert not decision["allowed"]
    assert "Inconsistent" in decision["reason"]


def test_reported_total_cannot_hide_inventory_not_accounted_for_as_free_or_used():
    balances = funded()
    totals = valuation(balances)
    balances["binance"]["BTC"]["total"] = "0.006"
    assert not snapshot(balances, totals, 100)["allowed"]


def test_small_float_summary_noise_does_not_relax_the_cap():
    balances = funded()
    totals = valuation(balances)
    totals["total_usdt"] = 1000.0000000000001
    decision = snapshot(balances, totals, 40, "100.00000000000001")
    assert not decision["allowed"]
    assert decision["total_equity_usdt"] == 1000


@pytest.mark.parametrize("cap,pending", [(101, 0), (-1, 0), (None, 0), ("NaN", 0),
                                        (50, -1), (50, None), (50, "Infinity"),
                                        (True, 0), (50, True)])
def test_invalid_limit_or_pending_notional_fails_closed(cap, pending):
    balances = funded()
    assert not snapshot(balances, valuation(balances), cap, pending)["allowed"]


def test_empty_equity_cannot_authorize_a_new_entry():
    balances = {"binance": {"USDT": asset()}}
    decision = snapshot(balances, valuation(balances), 50)
    assert not decision["allowed"]
    assert "equity must be positive" in decision["reason"]


def test_finite_but_unrepresentable_measurements_fail_closed():
    balances = {"binance": {"USDT": asset("1e999")}}
    decision = snapshot(balances, valuation(balances), 50)
    assert not decision["allowed"]
    json.dumps(decision, allow_nan=False)


def test_evaluation_is_pure_and_report_is_json_safe():
    balances = funded()
    totals = valuation(balances)
    before = deepcopy((balances, totals))
    decision = snapshot(balances, totals, "40", Decimal("0.1"))
    assert (balances, totals) == before
    assert json.loads(json.dumps(decision, allow_nan=False)) == decision
